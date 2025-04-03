# Copyright 2023-2024 SGLang Team
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
# Adapted from
# https://github.com/vllm-project/vllm/blob/main/vllm/model_executor/models/nemotron_nas.py

"""Inference-only deci model compatible with HuggingFace weights."""
from typing import Iterable, Optional, Set, Tuple, Type, Union

import torch
from torch import nn
from transformers import LlamaConfig


from sglang.srt.distributed import get_pp_group
from sglang.srt.layers.layernorm import RMSNorm
from sglang.srt.layers.logits_processor import LogitsProcessor
from sglang.srt.layers.quantization import QuantizationConfig
from sglang.srt.layers.sampler import Sampler #SamplerOutput, get_sampler
from sglang.srt.layers.vocab_parallel_embedding import (
    DEFAULT_VOCAB_PADDING_SIZE, ParallelLMHead, VocabParallelEmbedding)
from sglang.srt.model_loader.weight_utils import (
    default_weight_loader, maybe_remap_kv_scale_name)
from sglang.srt.models.llama import LlamaAttention, LlamaMLP
#from .interfaces import HasNoOps, SupportsLoRA, SupportsPP
#from vllm.compilation.decorators import support_torch_compile

## remove cacheconfig and vllmconfig to LlamaConfig
# from vllm.config import CacheConfig, VllmConfig
# from vllm.model_executor.sampling_metadata import SamplingMetadata
## import from utils
# from vllm.sequence import IntermediateTensors
# from .utils import (AutoWeightsLoader, PPMissingLayer, is_pp_missing_parameter,
#                     make_empty_intermediate_tensors_factory, make_layers,
#                     maybe_prefix)
from sglang.srt.utils import(AutoWeightsLoader, IntermediateTensors, 
                             make_empty_intermediate_tensors_factory,
                            PPMissingLayer, is_pp_missing_parameter,
                            make_layers, add_prefix) 

def _ffn_mult_to_intermediate_size(ffn_mult: float, n_embd: int) -> int:
    # DeciLM-specific code
    intermediate_size = int(2 * ffn_mult * n_embd / 3)
    return _find_multiple(intermediate_size, 256)


def _find_multiple(n: int, k: int) -> int:
    # DeciLM-specific code
    if n % k == 0:
        return n
    return n + k - (n % k)


class DeciLMDecoderLayer(nn.Module):

    def __init__(
        self,
        config: LlamaConfig,
        layer_idx: int,
        #cache_config,#: Optional[CacheConfig] = None,
        quant_config: Optional[QuantizationConfig] = None,
        prefix: str = "",
    ) -> None:
        super().__init__()
        block_config = config.block_configs[layer_idx]
        self._is_no_op_attention = block_config.attention.no_op
        self._is_no_op_ffn = block_config.ffn.no_op

        self.hidden_size = config.hidden_size
        rope_theta = getattr(config, "rope_theta", 10000)
        rope_scaling = getattr(config, "rope_scaling", None)
        if rope_scaling is not None and getattr(
                config, "original_max_position_embeddings", None):
            rope_scaling["original_max_position_embeddings"] = (
                config.original_max_position_embeddings)
        max_position_embeddings = getattr(config, "max_position_embeddings",
                                          8192)
        # Support abacusai/Smaug-72B-v0.1 with attention_bias
        # Support internlm/internlm-7b with bias
        rope_is_neox_style = getattr(config, "rope_is_neox_style", True)
        attention_bias = getattr(config, "attention_bias", False) or getattr(
            config, "bias", False)
        bias_o_proj = attention_bias
        # support internlm/internlm3-8b with qkv_bias
        if hasattr(config, "qkv_bias"):
            attention_bias = config.qkv_bias

        if not self._is_no_op_attention:
            num_kv_heads = (config.num_attention_heads //
                            block_config.attention.n_heads_in_group)
            # self.self_attn = LlamaAttention(
            #     config=config,
            #     hidden_size=self.hidden_size,
            #     num_heads=config.num_attention_heads,
            #     num_kv_heads=num_kv_heads,
            #     rope_theta=rope_theta,
            #     rope_scaling=rope_scaling,
            #     max_position_embeddings=max_position_embeddings,
            #     quant_config=quant_config,
            #     bias=attention_bias,
            #     bias_o_proj=bias_o_proj,
            #     cache_config=cache_config,
            #     prefix=f"{prefix}.self_attn",
            # )
            self.self_attn = LlamaAttention(
                config=config,
                hidden_size=self.hidden_size,
                num_heads=config.num_attention_heads,
                num_kv_heads=num_kv_heads,
                layer_id=layer_idx,
                rope_theta=rope_theta,
                rope_scaling=rope_scaling,
                rope_is_neox_style=rope_is_neox_style,
                max_position_embeddings=max_position_embeddings,
                quant_config=quant_config,
                prefix=add_prefix("self_attn", prefix),
                bias=attention_bias,
            )
            self.input_layernorm = RMSNorm(config.hidden_size,
                                           eps=config.rms_norm_eps)

        if not self._is_no_op_ffn:
            ffn_mult = block_config.ffn.ffn_mult
            intermediate_size = _ffn_mult_to_intermediate_size(
                ffn_mult, config.hidden_size)

            # self.mlp = LlamaMLP(
            #     hidden_size=self.hidden_size,
            #     intermediate_size=intermediate_size,
            #     hidden_act=config.hidden_act,
            #     quant_config=quant_config,
            #     bias=getattr(config, "mlp_bias", False),
            #     prefix=f"{prefix}.mlp",
            # )
            self.mlp = LlamaMLP(
                hidden_size=self.hidden_size,
                intermediate_size=intermediate_size,
                hidden_act=config.hidden_act,
                quant_config=quant_config,
                #bias=getattr(config, "mlp_bias", False),
                prefix=add_prefix("mlp", prefix),
            )
            self.post_attention_layernorm = RMSNorm(config.hidden_size,
                                                    eps=config.rms_norm_eps)

    def forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        intermediate_tensors: Optional[IntermediateTensors],
        residual: Optional[torch.Tensor],
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        # Self Attention

        if self._is_no_op_attention:
            pass
        else:
            if (residual is None):
                residual = hidden_states
                hidden_states = self.input_layernorm(hidden_states)
            else:
                hidden_states, residual = self.input_layernorm(
                    hidden_states, residual)
            hidden_states = self.self_attn(
                positions=positions,
                hidden_states=hidden_states,
                forward_batch=intermediate_tensors
            )

        # Fully Connected
        if not self._is_no_op_ffn:
            hidden_states, residual = self.post_attention_layernorm(
                hidden_states, residual)
            hidden_states = self.mlp(hidden_states)
        return hidden_states, residual


#@support_torch_compile
class DeciModel(nn.Module):

    # def __init__(
    #     self,
    #     *,
    #     vllm_config: VllmConfig,
    #     prefix: str = "",
    #     layer_type: Type[DeciLMDecoderLayer] = DeciLMDecoderLayer,
    # ):
    def __init__(
        self,
        *,
        config: LlamaConfig,
        quant_config: Optional[QuantizationConfig] = None,
        prefix: str = "",
        layer_type: Type[DeciLMDecoderLayer] = DeciLMDecoderLayer,
    ):
        super().__init__()

        #config = vllm_config.model_config.hf_config
        # skip cache_config
        #cache_config = vllm_config.cache_config
        #quant_config = vllm_config.quant_config
        #lora_config = vllm_config.lora_config
        lora_config = None    
        self.config = config
        self.quant_config = quant_config
        self.padding_idx = config.pad_token_id
        lora_vocab = ((lora_config.lora_extra_vocab_size *
                       (lora_config.max_loras or 1)) if lora_config else 0)
        self.vocab_size = config.vocab_size + lora_vocab
        self.org_vocab_size = config.vocab_size
        if get_pp_group().is_first_rank or (config.tie_word_embeddings
                                            and get_pp_group().is_last_rank):
            self.embed_tokens = VocabParallelEmbedding(
                self.vocab_size,
                config.hidden_size,
                org_num_embeddings=config.vocab_size,
                quant_config=quant_config,
            )
        else:
            self.embed_tokens = PPMissingLayer()

        def get_layer(idx: int, prefix: str):
            layer_idx = int(prefix.rsplit(".", 1)[1])
            return layer_type(
                config,
                layer_idx=idx,
                #cache_config,
                quant_config=quant_config,
                prefix=prefix,
            )

        # self.start_layer, self.end_layer, self.layers = make_layers(
        #     config.num_hidden_layers,
        #     get_layer,
        #     prefix=add_prefix("layers",prefix)
        # )
        self.layers = make_layers(
            config.num_hidden_layers,
            get_layer,
            prefix=add_prefix("layers",prefix)
        )
        if get_pp_group().is_last_rank:
            self.norm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        else:
            self.norm = PPMissingLayer()

        self.make_empty_intermediate_tensors = (
            make_empty_intermediate_tensors_factory(
                ["hidden_states", "residual"], config.hidden_size))


    def get_input_embeddings(self, input_ids: torch.Tensor) -> torch.Tensor:
        return self.embed_tokens(input_ids)

    def forward(
        self,
        input_ids: Optional[torch.Tensor],
        positions: torch.Tensor,
        intermediate_tensors: Optional[IntermediateTensors],
        inputs_embeds: Optional[torch.Tensor] = None,
    ) -> Union[torch.Tensor, IntermediateTensors]:
        if get_pp_group().is_first_rank:
            if inputs_embeds is not None:
                hidden_states = inputs_embeds
            else:
                hidden_states = self.get_input_embeddings(input_ids)
            residual = None
        else:
            assert intermediate_tensors is not None
            hidden_states = intermediate_tensors["hidden_states"]
            residual = intermediate_tensors["residual"]

        kv_cache_index = 0
        #for i in range(self.start_layer, self.end_layer):
        for i in range(len(self.layers)):
            layer = self.layers[i]
            if not layer._is_no_op_attention:
                hidden_states, residual = layer(positions, hidden_states,
                                                intermediate_tensors, residual)
                kv_cache_index += 1
            else:
                hidden_states, residual = layer(positions, hidden_states,
                                                intermediate_tensors, residual)

        if not get_pp_group().is_last_rank:
            return IntermediateTensors({
                "hidden_states": hidden_states,
                "residual": residual
            })

        hidden_states, _ = self.norm(hidden_states, residual)
        return hidden_states

    def load_weights(self, weights: Iterable[Tuple[str,
                                                   torch.Tensor]]) -> Set[str]:
        stacked_params_mapping = [
            # (param_name, shard_name, shard_id)
            (".qkv_proj", ".q_proj", "q"),
            (".qkv_proj", ".k_proj", "k"),
            (".qkv_proj", ".v_proj", "v"),
            (".gate_up_proj", ".gate_proj", 0),
            (".gate_up_proj", ".up_proj", 1),
        ]
        params_dict = dict(self.named_parameters())
        loaded_params: Set[str] = set()
        for name, loaded_weight in weights:
            if "rotary_emb.inv_freq" in name:
                continue
            if ("rotary_emb.cos_cached" in name
                    or "rotary_emb.sin_cached" in name):
                # Models trained using ColossalAI may include these tensors in
                # the checkpoint. Skip them.
                continue
            if self.quant_config is not None and (
                    scale_name := self.quant_config.get_cache_scale(name)):
                # Loading kv cache quantization scales
                param = params_dict[scale_name]
                weight_loader = getattr(param, "weight_loader",
                                        default_weight_loader)
                loaded_weight = (loaded_weight if loaded_weight.dim() == 0 else
                                 loaded_weight[0])
                weight_loader(param, loaded_weight)
                loaded_params.add(scale_name)
                continue
            if "scale" in name:
                # Remapping the name of FP8 kv-scale.
                name = maybe_remap_kv_scale_name(name, params_dict)
                if name is None:
                    continue
            for param_name, weight_name, shard_id in stacked_params_mapping:
                if weight_name not in name:
                    continue
                name = name.replace(weight_name, param_name)
                # Skip loading extra bias for GPTQ models.
                if name.endswith(".bias") and name not in params_dict:
                    continue

                if is_pp_missing_parameter(name, self):
                    continue

                param = params_dict[name]
                weight_loader = param.weight_loader
                weight_loader(param, loaded_weight, shard_id)
                break
            else:
                # Skip loading extra bias for GPTQ models.
                if name.endswith(".bias") and name not in params_dict:
                    continue

                if is_pp_missing_parameter(name, self):
                    continue

                param = params_dict[name]
                weight_loader = getattr(param, "weight_loader",
                                        default_weight_loader)
                weight_loader(param, loaded_weight)
            loaded_params.add(name)
        return loaded_params


class DeciLMForCausalLM(nn.Module): #, SupportsLoRA, SupportsPP, HasNoOps):
    packed_modules_mapping = {
        "qkv_proj": ["q_proj", "k_proj", "v_proj"],
        "gate_up_proj": ["gate_proj", "up_proj"],
    }

    # LoRA specific attributes
    supported_lora_modules = [
        "qkv_proj",
        "o_proj",
        "gate_up_proj",
        "down_proj",
        "embed_tokens",
        "lm_head",
    ]
    embedding_modules = {
        "embed_tokens": "input_embeddings",
        "lm_head": "output_embeddings",
    }
    embedding_padding_modules = ["lm_head"]

    # Mistral/Llama models can also be loaded with --load-format mistral
    # from consolidated.safetensors checkpoints
    mistral_mapping = {
        "layers": "model.layers",
        "attention": "self_attn",
        "wq": "q_proj",
        "wk": "k_proj",
        "wv": "v_proj",
        "wo": "o_proj",
        "attention_norm": "input_layernorm",
        "feed_forward": "mlp",
        "w1": "gate_proj",
        "w2": "down_proj",
        "w3": "up_proj",
        "ffn_norm": "post_attention_layernorm",
        "tok_embeddings": "model.embed_tokens",
        "output": "lm_head",
        "norm": "model.norm",
    }

    # def __init__(self, *, vllm_config: VllmConfig, prefix: str = ""):
    def __init__(self, *, 
                 config: LlamaConfig, 
                 quant_config: Optional[QuantizationConfig] = None,
                 prefix: str = ""):
        super().__init__()
        # This is already model_config.hf_config
        #config = vllm_config.model_config.hf_config 
        # Take this from augments
        #quant_config = vllm_config.quant_config
        # None here
        #lora_config = vllm_config.lora_config
        lora_config = None
        self.config = config
        self.lora_config = lora_config

        #self.model = self._init_model(vllm_config=vllm_config, 
        #                               prefix=maybe_prefix(prefix, "model"))
        self.model = self._init_model(config=config, quant_config=quant_config,
                                     prefix=add_prefix(prefix, "model"))
    
        if get_pp_group().is_last_rank:
            self.unpadded_vocab_size = config.vocab_size
            if lora_config:
                self.unpadded_vocab_size += lora_config.lora_extra_vocab_size
            self.lm_head = ParallelLMHead(
                self.unpadded_vocab_size,
                config.hidden_size,
                org_num_embeddings=config.vocab_size,
                padding_size=(
                    DEFAULT_VOCAB_PADDING_SIZE
                    # We need bigger padding if using lora for kernel
                    # compatibility
                    if not lora_config else
                    lora_config.lora_vocab_padding_size),
                quant_config=quant_config,
                prefix=add_prefix(prefix, "lm_head"),
            )
            if config.tie_word_embeddings:
                self.lm_head = self.lm_head.tie_weights(
                    self.model.embed_tokens)

            logit_scale = getattr(config, "logit_scale", 1.0)
            self.logits_processor = LogitsProcessor(self.unpadded_vocab_size,
                                                    config.vocab_size,
                                                    logit_scale)
        else:
            self.lm_head = PPMissingLayer()

        self.sampler = Sampler()#get_sampler()

        self.make_empty_intermediate_tensors = (
            self.model.make_empty_intermediate_tensors)

    # def _init_model(self, vllm_config: VllmConfig, 
    #                 prefix: str = ""):
    #     return DeciModel(vllm_config=vllm_config, prefix=prefix)
    def _init_model(self, config: LlamaConfig, 
                    quant_config: Optional[QuantizationConfig] = None, 
                    prefix: str = ""):
        return DeciModel(config=config, quant_config=quant_config, prefix=prefix)

    def get_input_embeddings(self, input_ids: torch.Tensor) -> torch.Tensor:
        return self.model.get_input_embeddings(input_ids)

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        intermediate_tensors: Optional[IntermediateTensors] = None,
        inputs_embeds: Optional[torch.Tensor] = None,
    ) -> Union[torch.Tensor, IntermediateTensors]:
        model_output = self.model(input_ids, positions, intermediate_tensors,
                                  inputs_embeds)
        return model_output

    def compute_logits(
        self,
        hidden_states: torch.Tensor,
        sampling_metadata,#: SamplingMetadata,
    ) -> Optional[torch.Tensor]:
        # logits = self.logits_processor(self.lm_head, hidden_states,
        #                                sampling_metadata)
        logits = self.logits_processor(input_ids= None,
                                       hidden_states=hidden_states,
                                       lm_head=self.lm_head,
                                       logits_metadata=sampling_metadata)
        return logits

    def _preprocess_logits(
        self, logits_output,#: LogitsProcessorOutput, 
        sampling_info#: SamplingBatchInfo
    ):
        # Apply logit bias
        if sampling_info.sampling_info_done:
            # Overlap mode: the function update_regex_vocab_mask was executed
            # in process_batch_result of the last batch.
            if sampling_info.grammars:
                sampling_info.sampling_info_done.wait()
        else:
            # Normal mode: Put CPU-heavy tasks here. They will be overlapped with the forward pass.
            sampling_info.update_regex_vocab_mask()
        sampling_info.apply_logits_bias(logits_output.next_token_logits)

    def sample(
        self,
        logits_output,#: LogitsProcessorOutput,
        forward_batch#: ForwardBatch,
    ) -> torch.Tensor:
        """Sample and compute logprobs and update logits_output.

        Args:
            logits_output: The logits output from the model forward
            forward_batch: The forward batch that generates logits_output

        Returns:
            A list of next_token_ids
        """
        # For duplex models with multiple output streams.
        if isinstance(logits_output, tuple):
            return torch.stack(
                [self.sample(values, forward_batch) for values in logits_output],
                axis=-1,
            )

        self._preprocess_logits(logits_output, forward_batch.sampling_info)

        # Sample the next tokens
        next_token_ids = self.sampler(
            logits_output,
            forward_batch.sampling_info,
            forward_batch.return_logprob,
            forward_batch.top_logprobs_nums,
            forward_batch.token_ids_logprobs,
        )
        return next_token_ids

    # def sample(self, logits: torch.Tensor,
    #            sampling_metadata,#: SamplingMetadata
    # ):
    #     next_tokens = self.sampler(logits, sampling_metadata)
    #     return next_tokens

    def load_weights(self, weights: Iterable[Tuple[str,
                                                   torch.Tensor]]) -> Set[str]:
        loader = AutoWeightsLoader(
            self,
            skip_prefixes=(["lm_head."]
                           if self.config.tie_word_embeddings else None),
        )
        return loader.load_weights(weights)

EntryClass = [DeciLMForCausalLM]