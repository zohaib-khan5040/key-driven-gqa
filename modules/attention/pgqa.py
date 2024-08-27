import math
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F

from utils import assign_check

@staticmethod
def get_gaussian(inp_mat):
    """
    Generate a gaussian noise matrix of the same shape as inp_mat,
    with each row having the same mean and standard deviation as the corresponding row in inp_mat.
    The diagonal elements of the random matrix are set to zero. (Sparse noise helps)
    """
    # Get the shape of inp_mat
    _, x, y = inp_mat.shape
    
    # Calculate the mean and standard deviation for each row of inp_mat
    row_means = inp_mat.mean(dim=-1, keepdim=True)
    row_stds = inp_mat.std(dim=-1, keepdim=True)
    
    # Generate a random matrix of the same shape as inp_mat
    matrix = torch.rand(x, y, device=inp_mat.device)  # Ensure matrix is on the same device as inp_mat
    
    # Set the diagonal elements to zero
    diag_indices = torch.arange(min(x, y))
    matrix[diag_indices, diag_indices] = 0
    
    # Normalize each row of the random matrix to have the same mean and standard deviation as the corresponding row in inp_mat
    rand_means = matrix.mean(dim=-1, keepdim=True)
    rand_stds = matrix.std(dim=-1, keepdim=True)
    
    # Avoid division by zero by setting zero stds to 1 (if any)
    rand_stds[rand_stds == 0] = 1
    
    matrix = (matrix - rand_means) / rand_stds  # Standardize to mean 0, std 1
    matrix = matrix * row_stds + row_means      # Scale to target mean and std
    
    return matrix

class PGQA(nn.Module):

    def __init__(
            self,
            dim: int,
            num_heads: int = 8,
            qkv_bias: bool = False,
            attn_drop: float = 0.,
            proj_drop: float = 0.,
            num_kv_heads: Optional[int] = None,
    ) -> None:
        super().__init__()
        assert dim % num_heads == 0, 'dim should be divisible by num_heads'
        self.dim = dim
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.scale = self.head_dim ** -0.5
        self.num_kv_heads = num_kv_heads if num_kv_heads is not None else (num_heads // 2) # have at least two heads in each group

        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.k = nn.Linear(dim, self.num_kv_heads*self.head_dim, bias=qkv_bias)
        self.v = nn.Linear(dim, self.num_kv_heads*self.head_dim, bias=qkv_bias)

        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, P, C = x.shape
        H = self.num_heads
        q = self.q(x).view(B, P, H, -1).transpose(1, 2) # (B, H, P, head_size)
        k = self.k(x).view(B, P, self.num_kv_heads, -1).transpose(1, 2) # (B, num_kv_heads, P, head_size)
        v = self.v(x).view(B, P, self.num_kv_heads, -1).transpose(1, 2) # (B, num_kv_heads, P, head_size)
        
        q = q * self.scale

        group_size = self.num_heads // self.num_kv_heads
        q_grps = torch.split(q, group_size, dim=1)
        k_grps = torch.split(k, 1, dim=1) 
        v_grps = torch.split(v, 1, dim=1)

        outputs = [None] * len(k_grps)
        for i in range(len(k_grps)):
                
            # Collect items (note q has a larger head axis)
            curr_q = q_grps[i]  # (B, num_heads//num_kv_heads, num_patches, head_size)
            curr_k = k_grps[i]  # (B, 1, num_patches, head_size)
            curr_v = v_grps[i]  # (B, 1, num_patches, head_size)
            
            scores = (curr_q @ curr_k.transpose(-2, -1))
            weights = F.softmax(scores, dim=-1) # (B, num_heads//num_kv_heads, num_patches, num_patches)
            weights = self.attn_drop(weights)
            curr_att = weights @ curr_v # (B, num_heads//num_kv_heads, num_patches, head_size)
            
            ############ START OF Gaussian Noise CODE ############
            batch, groups, patches, head_size = curr_att.shape 
            curr_att = curr_att.view(batch, groups, -1)

            device = curr_att.device # Fetch and push the Gaussian matrix to same device
            gaussian = get_gaussian(curr_att).to(device)

            curr_att = curr_att - gaussian # Subtract normalised Gaussian noise
            curr_att = curr_att.view(batch, groups, patches, -1)
            ############# END OF Gaussian Noise CODE #############

            outputs[i] = curr_att

        x = torch.cat(outputs, dim=1) # (B, num_heads, num_patches, head_size)
        x = x.transpose(1, 2).contiguous().view(B, P, C) # (B, num_patches, emb_dim)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x
    
    def att_weight_conversion(self, qkv_params, is_bias=False):
        '''
        Split and convert the QKV parameters from ViT checkpoints for the GQA implementation
        '''
        q, k, v = torch.split(qkv_params, qkv_params.shape[0] // 3, dim=0)

        group_size = self.num_heads // self.num_kv_heads

        def convert_weight(param):
            x = param.clone() # (dim, dim)

            # This totally breaks if you reshape as (dim, H, dim/H) and split across dim=1
            # You have to shape it as (H, dim/H, dim) and split the across dim=0
            x = x.view(self.num_heads, self.dim//self.num_heads, self.dim)
            xs = torch.split(x, group_size, dim=0) # split across head axis
            xs = [xs[i].mean(dim=0) for i in range(self.num_kv_heads)]
            x = torch.cat(xs, dim=0)

            expected_shape = (self.num_kv_heads*self.dim//self.num_heads, self.dim)
            assert x.shape == expected_shape, f'Expected {expected_shape}, got {x.shape}'
            return x
        
        def convert_bias(param):
            x = param.clone()
            x = x.view(self.num_heads, self.dim//self.num_heads)
            xs = torch.split(x, group_size, dim=0) # split across head axis
            xs = [xs[i].mean(dim=0) for i in range(self.num_kv_heads)]
            x = torch.cat(xs, dim=0)

            expected_shape = (self.num_kv_heads*self.dim//self.num_heads,)
            assert x.shape == expected_shape, f'Expected {expected_shape}, got {x.shape}'
            return x
        
        return {
            "q": q,
            "k": convert_weight(k) if not is_bias else convert_bias(k),
            "v": convert_weight(v) if not is_bias else convert_bias(v)
        }
    
    def load_pretrained_weights(self, state_dict, block_idx):

        # Load in parameters for the Query Key Value layers
        qkv_weight = state_dict[f'blocks.{block_idx}.attn.qkv.weight']
        qkv_bias = state_dict[f'blocks.{block_idx}.attn.qkv.bias']

        wdict = self.att_weight_conversion(qkv_weight)
        bdict = self.att_weight_conversion(qkv_bias, is_bias=True)

        self.q.weight = assign_check(self.q.weight, wdict['q'])
        self.q.bias = assign_check(self.q.bias, bdict['q'])

        self.k.weight = assign_check(self.k.weight, wdict['k'])
        self.k.bias = assign_check(self.k.bias, bdict['k'])
        
        self.v.weight = assign_check(self.v.weight, wdict['v'])
        self.v.bias = assign_check(self.v.bias, bdict['v'])

        # Load in parameters for the output projection
        self.proj.weight = assign_check(self.proj.weight, state_dict[f'blocks.{block_idx}.attn.proj.weight'])
        self.proj.bias = assign_check(self.proj.bias, state_dict[f'blocks.{block_idx}.attn.proj.bias'])