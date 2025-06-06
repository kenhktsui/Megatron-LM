# Copyright (c) 2024, NVIDIA CORPORATION. All rights reserved.

"""Forward step utilities."""

from collections.abc import Iterable

import torch
import warnings

from megatron.core import mpu
from megatron.core.inference.contexts import BaseInferenceContext
from megatron.core.transformer.module import MegatronModule
from megatron.training import get_args

from .communication import recv_from_prev_pipeline_rank_, send_to_next_pipeline_rank


class ForwardStep:
    """Forward step function with all the communications.
    We use a class here to hide the inference parameters
    from the outside caller."""

    def __init__(
        self,
        model: MegatronModule,
        inference_context: BaseInferenceContext,
    ):
        """Set values so we don't need to do it multiple times."""
        # Make sure model is in eval mode.
        assert not isinstance(model, Iterable), \
            'interleaving schedule is not supported for inference'
        model.eval()
        self.model = model
        self.inference_context = inference_context
        # Pipelining arguments.
        args = get_args()
        self.pipeline_size_larger_than_one = (
            args.pipeline_model_parallel_size > 1)
        # Threshold for whether we split up the batch for pipelining.
        self.pipelining_batch_x_seqlen = \
            args.inference_batch_times_seqlen_threshold

    @property
    def inference_params(self):
        warnings.warn("`inference_params` renamed to `inference_context`, and will be removed in `megatron-core` 0.13.")
        return self.inference_context

    @inference_params.setter
    def inference_params(self, value):
        warnings.warn("`inference_params` renamed to `inference_context`, and will be removed in `megatron-core` 0.13.")
        self.inference_context = value

    def _forward(self, tokens, position_ids, attention_mask):
        return self.model(tokens, position_ids, attention_mask, inference_context=self.inference_context)

    def __call__(self, tokens, position_ids, attention_mask, recv_buffer_seq_length=None):
        """Invocation of the forward methods. Note that self.inference_context
        is being modified by the forward step."""
        # Pipelining case.
        # This runs only if current_batch_x_seqlen > args.inference_batch_times_seqlen_threshold
        # and requires setting args.pipeline_model_parallel > 1. The batch will be split into
        # smaller microbatches to be pipelined through the stages.
        if self.pipeline_size_larger_than_one and self.pipelining_batch_x_seqlen != -1:
            seq_len = tokens.size(1) if recv_buffer_seq_length is None else recv_buffer_seq_length
            current_batch_x_seqlen = tokens.size(0) * seq_len
            if current_batch_x_seqlen >= self.pipelining_batch_x_seqlen:
                micro_batch_size = \
                    max(1, self.pipelining_batch_x_seqlen // seq_len)
                return self._with_pipelining_forward_step(tokens,
                                                          position_ids,
                                                          attention_mask,
                                                          micro_batch_size,
                                                          recv_buffer_seq_length=recv_buffer_seq_length)

        recv_buffer = None
        if recv_buffer_seq_length is not None:
            recv_buffer = _allocate_recv_buffer(tokens.size(0), recv_buffer_seq_length)

        return self._no_pipelining_forward_step(tokens,
                                                position_ids,
                                                attention_mask,
                                                recv_buffer=recv_buffer)


    def _forward_step_helper(self, tokens, position_ids, attention_mask, recv_buffer=None):
        """Single forward step. Update the allocate memory flag so
        only the first time the memory is allocated."""
        batch_size = tokens.size(0)
        sequence_length = tokens.size(1)

        if recv_buffer is None:
            recv_buffer = _allocate_recv_buffer(batch_size, sequence_length)

        # Receive from previous stage.
        if recv_buffer is not None and torch.numel(recv_buffer) > 0:
            recv_from_prev_pipeline_rank_(recv_buffer)

        # Forward pass through the model.
        if not mpu.is_pipeline_first_stage():
            self.model.set_input_tensor(recv_buffer)
        output_tensor = self._forward(tokens, position_ids, attention_mask)
        if isinstance(output_tensor, tuple):
            output_tensor = output_tensor[0]

        # Send output to the next stage.
        send_to_next_pipeline_rank(output_tensor)

        return output_tensor



    def _no_pipelining_forward_step(self, tokens, position_ids, attention_mask,
                                    recv_buffer=None):
        """If recv_buffer is none, we will allocate one on the fly."""
        # Run a simple forward pass.
        output_tensor = self._forward_step_helper(tokens, position_ids,
                                                  attention_mask, recv_buffer=recv_buffer)
        # Update the sequence length offset.
        self.inference_context.sequence_len_offset += tokens.size(1)

        logits = None
        if mpu.is_pipeline_last_stage():
            logits = output_tensor

        return logits


    def _with_pipelining_forward_step(self, tokens, position_ids, attention_mask, micro_batch_size, recv_buffer_seq_length=None):
        """No interleaving is supported."""
        batch_size = tokens.size(0)
        sequence_length = tokens.size(1) if recv_buffer_seq_length is None else recv_buffer_seq_length

        # Divide the batch dimension into micro batches.
        num_micro_batches, last_chunk = divmod(batch_size,
                                            micro_batch_size)
        if last_chunk > 0:
            num_micro_batches += 1

        # Preallocate memory for output logits.
        logits = None
        if mpu.is_pipeline_last_stage():
            args = get_args()
            logits = torch.empty(
                (batch_size, sequence_length, args.padded_vocab_size),
                dtype=torch.float32, device=torch.cuda.current_device())

        # Preallocate recv buffer.
        recv_buffer = _allocate_recv_buffer(micro_batch_size, sequence_length)

        for micro_batch_index in range(num_micro_batches):
            # Slice among the batch dimenion.
            start = micro_batch_index * micro_batch_size
            end = min(start + micro_batch_size, batch_size)
            this_micro_batch_size = end - start
            tokens2use = tokens[start:end, ...]
            position_ids2use = position_ids[start:end, ...]

            # Run a simple forward pass.
            if this_micro_batch_size != micro_batch_size:
                recv_buffer = None
            output = self._forward_step_helper(tokens2use, position_ids2use, attention_mask, recv_buffer=recv_buffer)

            # Adjust the batch size offset to account for the micro-batch.
            self.inference_context.batch_size_offset += this_micro_batch_size

            # Copy logits.
            if mpu.is_pipeline_last_stage():
                logits[start:end, ...] = output

        # Once we are done with all the micro-batches, we can
        # adjust the sequence length offset.
        self.inference_context.sequence_len_offset += tokens.size(1)
        # and reset the batch size offset
        self.inference_context.batch_size_offset = 0

        return logits


def _get_recv_buffer_dtype(args):
    """Receive happens between the layers."""
    if args.fp32_residual_connection:
        return torch.float
    return args.params_dtype

def _allocate_recv_buffer(batch_size, sequence_length):
    """Receive happens between the layers with size [s, b, h]."""
    if mpu.is_pipeline_first_stage():
        return None
    args = get_args()
    recv_size = (sequence_length, batch_size, args.hidden_size)
    return torch.empty(recv_size,
                       dtype=_get_recv_buffer_dtype(args),
                       device=torch.cuda.current_device())
