# 基础的 package
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional, Tuple
import math
from dataclasses import dataclass

# 一些前置代码，本次课暂时不涉及；
# 如果有需要，以后可以专门出视频讲解
class DeepseekV2RMSNorm(nn.Module):
   def __init__(self, hidden_size, eps=1e-6):
      super().__init__()
      self.weight = nn.Parameter(torch.ones(hidden_size))
      self.variance_epsilon = eps

   def forward(self, hidden_states):
      input_dtype = hidden_states.dtype
      hidden_states = hidden_states.to(torch.float32)
      variance = hidden_states.pow(2).mean(-1, keepdim=True)
      hidden_states = hidden_states * torch.rsqrt(variance + self.variance_epsilon)
      return self.weight * hidden_states.to(input_dtype)


class DeepseekV2RotaryEmbedding(nn.Module):
   def __init__(self, dim, max_position_embeddings=2048, base=10000, device=None):
      super().__init__()

      self.dim = dim
      self.max_position_embeddings = max_position_embeddings
      self.base = base
      inv_freq = 1.0 / (
              self.base ** (torch.arange(0, self.dim, 2).float().to(device) / self.dim)
      )
      self.register_buffer("inv_freq", inv_freq, persistent=False)
      # 较小索引位置对应较低频率
      # 较大的索引位置有较高的频率

      # Build here to make `torch.jit.trace` work.
      self._set_cos_sin_cache(
         seq_len=max_position_embeddings,
         device=self.inv_freq.device,
         dtype=torch.get_default_dtype(),
      )
      self.max_seq_len_cached = None

   def _set_cos_sin_cache(self, seq_len, device, dtype):
      self.max_seq_len_cached = seq_len
      t = torch.arange(
         self.max_seq_len_cached, device=device, dtype=self.inv_freq.dtype
      )

      freqs = torch.outer(t, self.inv_freq.to(t.device))
      # Different from paper, but it uses a different permutation in order to obtain the same calculation
      emb = torch.cat((freqs, freqs), dim=-1)
      self.register_buffer("cos_cached", emb.cos().to(dtype), persistent=False)
      self.register_buffer("sin_cached", emb.sin().to(dtype), persistent=False)

   def forward(self, x, seq_len=None):
      # x: [bs, num_attention_heads, seq_len, head_size]
      if self.max_seq_len_cached is None or seq_len > self.max_seq_len_cached:
         self._set_cos_sin_cache(seq_len=seq_len, device=x.device, dtype=x.dtype)

      return (
         self.cos_cached[:seq_len].to(dtype=x.dtype),
         self.sin_cached[:seq_len].to(dtype=x.dtype),
      )


# Copied from transformers.models.llama.modeling_llama.rotate_half
def rotate_half(x):
   """Rotates half the hidden dims of the input."""
   x1 = x[..., : x.shape[-1] // 2]
   x2 = x[..., x.shape[-1] // 2:]
   return torch.cat((-x2, x1), dim=-1)


# Copied from transformers.models.llama.modeling_llama.apply_rotary_pos_emb
def apply_rotary_pos_emb(q, k, cos, sin, position_ids, unsqueeze_dim=1):
   cos = cos[position_ids].unsqueeze(unsqueeze_dim)
   sin = sin[position_ids].unsqueeze(unsqueeze_dim)

   b, h, s, d = q.shape
   q = q.view(b, h, s, d // 2, 2).transpose(4, 3).reshape(b, h, s, d)

   b, h, s, d = k.shape
   k = k.view(b, h, s, d // 2, 2).transpose(4, 3).reshape(b, h, s, d)

   q_embed = (q * cos) + (rotate_half(q) * sin)
   k_embed = (k * cos) + (rotate_half(k) * sin)
   return q_embed, k_embed

@dataclass
class DeepseekConfig:
   hidden_size: int
   num_heads: int
   max_position_embeddings: int  # 这是rope 相关的参数
   rope_theta: float  # 频率，一般设置的比较大

   attention_dropout: float

   q_lora_rank: int  # latent 的shape , 一般设置比较大一点；10000+
   qk_rope_head_dim: int  # 64
   kv_lora_rank: int  # 公式 41, 可能是 512

   v_head_dim: int  # 128
   qk_nope_head_dim: int
   attention_bias: bool


class MLA(nn.Module):
   def __init__(self, config: DeepseekConfig):
      super().__init__()
      # 三个部分；
      # part1 , mha 部分

      # 对应 query 压缩的向量， 在 deepseek v3 中， hidden_size 7168
      # 但是压缩后的 kv d_c= 512，压缩比例 1/14
      # q 的压缩为 1536 压缩比例 1/4.7
      # rope 部分是 64

      self.attention_dropout = config.attention_dropout
      self.hidden_size = config.hidden_size #7168
      self.num_heads = config.num_heads #16
      self.v_head_dim = config.v_head_dim #128

      #16 * 128 ->7168
      self.out_proj = nn.Linear(
         self.num_heads * self.v_head_dim,
         self.hidden_size,
         bias=False,  # 可以加，也可以不加
      )

      # 最重要的是 part2
      # MLA 压缩部分
      # p2.1 down 压缩
      self.qk_nope_head_dim = config.qk_nope_head_dim #128
      self.qk_rope_head_dim = config.qk_rope_head_dim #64

      self.q_lora_rank = config.q_lora_rank  #1536
      # 一般会从 7168 -> 1536； 压缩比是 1/4.7

      self.kv_lora_rank = config.kv_lora_rank #512
      # 包含两个部分

      # q降维从7168->1536, 压缩比是 1/4.7
      self.q_down_proj = nn.Linear(
         self.hidden_size,
         self.q_lora_rank,
         bias=config.attention_bias,
      )
      self.q_down_norm = DeepseekV2RMSNorm(self.q_lora_rank)
      # kv降维从7168->(512+64),其中512是kv的降维nope部分，64是qk降维的rope部分
      self.kv_down_proj = nn.Linear(
         self.hidden_size,
         self.kv_lora_rank + config.qk_rope_head_dim,
         bias=config.attention_bias,
      )  # qk_rope_head_dim 这个一般设置的很小，一般是 64
      self.kv_down_norm = DeepseekV2RMSNorm(self.kv_lora_rank)
      # down 之后包含了两个部分，要做 split

      # p2.2 升维
      # q and k shape is same
      self.q_head_dim = config.qk_nope_head_dim + config.qk_rope_head_dim
      # q部分升维: 从1536-> 16个头 * （nope部分:128 + rope部分:64 ）
      self.q_up_proj = nn.Linear(
         self.q_lora_rank,
         self.num_heads * self.q_head_dim,
         bias=config.attention_bias,
      )  # 这里也要 split

      # kv部分升维: 前面从7168->(512+64), 后面要split取nope部分512
      # 这里升维512->16个头 * （k nope部分:128 + v nope部分:128 ）
      # 升维后得到K与V，在split
      self.kv_up_proj = nn.Linear(
         self.kv_lora_rank,
         self.num_heads * (
                 self.q_head_dim - config.qk_rope_head_dim + self.v_head_dim
         ),  # self.q_head_dim - config.qk_rope_head_dim = nope_shape ,
         bias=config.attention_bias,
      )

      # part3: rope部分
      self.rotary_emb = DeepseekV2RotaryEmbedding(
         config.qk_rope_head_dim,
         config.max_position_embeddings,
         config.rope_theta,
      )

      # part4: kv cache 的实现，下次课来讲，本次课先不管

   def forward(self, hidden_states, position_ids, attention_mask=None, ):
      # hidden_states (b, seq_len, hidden_dim)
      bsz, q_len, _ = hidden_states.size()

      # 1. q compression 从7168->1536
      q = self.q_down_proj(
         hidden_states
      )
      q = self.q_down_norm(q)
      #从1536-> 16个头 * （nope部分:128 + rope部分:64 ）
      q = self.q_up_proj(q)

      # q shape 是什么：self.num_heads * self.q_head_dim,
      # (b, seq_len, self.num_heads * self.q_head_dim,)
      q = q.view(
         bsz, q_len, self.num_heads, self.q_head_dim
      ).transpose(1, 2)
      # （b, num_head, seq_len, q_head_dim）

      #拆分为q的nope部分(128)与q的rope部分(64)
      q_nope, q_rope = torch.split(
         q,
         [self.qk_nope_head_dim, self.qk_rope_head_dim],
         dim=-1
      )

      # kv part 从7168->(512+64),其中512是kv的降维nope部分，64是qk降维的rope部分
      c_kv = self.kv_down_proj(hidden_states)
      #拆分维kv降维部分512与k的rope部分64
      c_kv, k_rope = torch.split(
         c_kv,
         [self.kv_lora_rank, self.qk_rope_head_dim],  # self.kv_lora_rank + config.qk_rope_head_dim
         dim=-1,
      )  # k_rope 的 shape （b, seq_len, elf.qk_rope_head_dim）
      k_rope = k_rope.view(
         bsz, q_len, 1, self.qk_rope_head_dim,
      ).transpose(1, 2)  # boradcast
      # (b, 1, seq_len, qk_rope_head_dim）)

      kv = self.kv_down_norm(c_kv)
      #升维512->16个头 * （k nope部分:128 + v nope部分:128 ）
      kv = self.kv_up_proj(kv)

      # （b, seq, num_head * (self.q_head_dim - config.qk_rope_head_dim + self.v_head_dim)
      # 每个头（128 + 128）
      kv = kv.view(
         bsz, q_len, self.num_heads,
         self.qk_nope_head_dim + self.v_head_dim
      ).transpose(1, 2)

      #拆分到K的nope部分与V的部分
      k_nope, value_states = torch.split(
         kv,
         [self.qk_nope_head_dim, self.v_head_dim],
         dim=-1,
      )

      print("V:", value_states.shape)

      # apply 位置编码 rope
      kv_seq_len = value_states.shape[-2]
      # value_states shape (b, nums_head, seq_len, v_head_dim)

      # 怎么使用 rope 下次课讲 ROPE 的时候来讲
      cos, sin = self.rotary_emb(
         value_states, seq_len=kv_seq_len,
      )
      #得到q与k的位置编码部分
      q_rope, k_rope = apply_rotary_pos_emb(
         q_rope, k_rope, cos, sin, position_ids,
      )

      # MHA
      query_states = torch.concat(
         [q_nope, q_rope], dim=-1
      )
      key_states = torch.concat(
         [k_nope, k_rope.expand(-1, self.num_heads, -1, -1)], dim=-1
      )  # (b, 1, seq_len, dim)
      # shape is( b, num_head,
      #  q_len, head_dim = self.qk_nope_head_dim + self.qk_rope_head_dim)
      #（batch, heads, seq_len, head_dim) -> [2, 16, 1024, 192]
      print("Q:", query_states.shape)
      print("K:", key_states.shape)

      # MHA 无数遍了 [2, 16, 1024, 192] * [2, 16, 1024, 192] 后两维相乘- > [2, 16, 1024, 1024]
      attn_weights = torch.matmul(
         query_states, key_states.transpose(2, 3)
      )
      print("W:", attn_weights.shape)

      attn_weights = attn_weights / math.sqrt(self.q_head_dim)

      if attention_mask is not None:
         # causal mask #
         attn_weights = torch.masked_fill(
            attn_weights,
            attention_mask == 0,
            float('-inf')
         )

      # softmax 以及 output proj
      attn_weights = F.softmax(
         attn_weights, dim=-1
      ).to(query_states.dtype)

      attn_weights = F.dropout(
         attn_weights, p=self.attention_dropout,
         training=self.training,
      )

      output = torch.matmul(
         # (b, num_head, q_len, q_len)
         # value (b, nums_head, seq_len, v_head_dim)
         attn_weights, value_states
      )
      output = output.transpose(1, 2).reshape(bsz, q_len, -1)

      # （b, q_len, v_dim * num_head） 16 * 128 ->7168
      output = self.out_proj(
         output
      )
      print("O:", output.shape)
      return output, attn_weights


# 写一个测试函数
def test_mla():
   config = DeepseekConfig(
      hidden_size=7168,
      num_heads=16,
      max_position_embeddings=1024,
      rope_theta=128000,
      attention_dropout=0.1,
      q_lora_rank=1536,
      qk_rope_head_dim=64,
      kv_lora_rank=512,

      v_head_dim=128,
      qk_nope_head_dim=128,
      attention_bias=False,
   )

   mla = MLA(config)
   x = torch.randn(2, 1024, 7168)
   position_ids = torch.arange(
      config.max_position_embeddings,
   ).unsqueeze(0).expand(
      x.size(0), -1
   )  # (batch_size, seq_len)
   attn_output, attn_weights = mla(x, position_ids=position_ids)
   print(attn_output.shape)
   print(attn_weights.shape)

if __name__ == '__main__':
   test_mla()