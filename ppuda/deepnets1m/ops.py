# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
Some functionality in this script is based on:
- https://github.com/quark0/darts (Apache License 2.0)
- https://github.com/ai-med/squeeze_and_excitation/blob/master/squeeze_and_excitation/squeeze_and_excitation.py (MIT License)
- https://github.com/lucidrains/vit-pytorch/blob/main/vit_pytorch/vit.py (MIT License)

"""


import torch
import torchvision
import torch.nn as nn
from einops import rearrange
from .light_ops import *


def parse_op_ks(op):
    ks = 0
    op_name = ''
    is_double = False
    for s in op.split('_'):
        ks_str = s.split('x')
        valid_ks_str = False
        if len(ks_str) == 2:
            if ks > 0:
                is_double = True
            for i in range(2):
                try:
                    ks = max(ks, int(ks_str[i]))
                    valid_ks_str = True
                except:
                    continue

        if not valid_ks_str:
            if len(op_name) > 0:
                op_name += '_'
            op_name += s
    if is_double:
        op_name += '2'
    return op_name, ks


def bn_layer(norm, C, light):
    if norm in [None, '', 'none']:
        norm_layer = nn.Identity()
    elif norm.startswith('bn'):
        if light:
            norm_layer = BatchNorm2dLight(C,
                                           track_running_stats=norm.find('track') >= 0)
        else:
            norm_layer = nn.BatchNorm2d(C,
                                        track_running_stats=norm.find('track') >= 0)
    else:
        raise NotImplementedError(norm)
    return norm_layer


def ln_layer(C, light):
    return LayerNormLight(C) if light else nn.LayerNorm(C)


def conv_layer(light):
    return Conv2dLight if light else nn.Conv2d


def lin_layer(light):
    return LinearLight if light else nn.Linear


NormLayers = [nn.BatchNorm2d, nn.LayerNorm, BatchNorm2dLight, LayerNormLight]
try:
    import torchvision
    NormLayers.append(torchvision.models.convnext.LayerNorm2d)
except Exception as e:
    print(e, 'convnext requires torchvision >= 0.12, current version is ', torchvision.__version__)


OPS = {
    'none' : lambda C_in, C_out, ks, stride, norm, light: Zero(stride),
    'skip_connect' : lambda C_in, C_out, ks, stride, norm, light: nn.Identity() if stride == 1 else FactorizedReduce(C_in, C_out, norm=norm, light=light),
    'avg_pool' : lambda C_in, C_out, ks, stride, norm, light: nn.AvgPool2d(ks, stride=stride, padding=ks // 2, count_include_pad=False),
    'max_pool' : lambda C_in, C_out, ks, stride, norm, light: nn.MaxPool2d(ks, stride=stride, padding=ks // 2),
    'conv' : lambda C_in, C_out, ks, stride, norm, light: ReLUConvBN(C_in, C_out, ks, stride, ks // 2, norm, light=light),
    'sep_conv' : lambda C_in, C_out, ks, stride, norm, light: SepConv(C_in, C_out, ks, stride, ks // 2, norm=norm, light=light),
    'dil_conv' : lambda C_in, C_out, ks, stride, norm, light: DilConv(C_in, C_out, ks, stride, ks - ks % 2, 2, norm=norm, light=light),
    'conv2' : lambda C_in, C_out, ks, stride, norm, light: ReLUConvBN(C_in, C_out, ks, stride, ks // 2, norm, double=True, light=light),
    'conv_stride' : lambda C_in, C_out, ks, stride, norm, light: conv_layer(light)(C_in, C_out, ks, stride=ks, bias=False, padding=int(ks < 4)),
    'msa':  lambda C_in, C_out, ks, stride, norm, light: Transformer(C_in, dim_out=C_out, stride=stride, light=light),
    'cse':  lambda C_in, C_out, ks, stride, norm, light: ChannelSELayer(C_in, dim_out=C_out, stride=stride, light=light),
}


class ChannelSELayer(nn.Module):
    """
    Copied from https://github.com/ai-med/squeeze_and_excitation/blob/master/squeeze_and_excitation/squeeze_and_excitation.py

    Re-implementation of Squeeze-and-Excitation (SE) block described in:
        *Hu et al., Squeeze-and-Excitation Networks, arXiv:1709.01507*

    MIT License

    Copyright (c) 2018 Abhijit Guha Roy

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

    """

    def __init__(self, num_channels, reduction_ratio=2, dim_out=None, stride=1, light=False):
        """
        :param num_channels: No of input channels
        :param reduction_ratio: By how much should the num_channels should be reduced
        """
        super(ChannelSELayer, self).__init__()
        if dim_out is not None:
            assert dim_out == num_channels, (dim_out, num_channels, 'only same dimensionality is supported')
        num_channels_reduced = num_channels // reduction_ratio
        self.reduction_ratio = reduction_ratio
        self.stride = stride
        self.fc1 = lin_layer(light)(num_channels, num_channels_reduced, bias=True)
        self.fc2 = lin_layer(light)(num_channels_reduced, num_channels, bias=True)
        self.relu = nn.ReLU()
        self.sigmoid = nn.Hardswish()

    def forward(self, input_tensor):
        """
        :param input_tensor: X, shape = (batch_size, num_channels, H, W)
        :return: output tensor
        """
        batch_size, num_channels, H, W = input_tensor.size()
        # Average along each channel
        squeeze_tensor = input_tensor.reshape(batch_size, num_channels, -1).mean(dim=2)

        # channel excitation
        fc_out_1 = self.relu(self.fc1(squeeze_tensor))
        fc_out_2 = self.sigmoid(self.fc2(fc_out_1))

        a, b = squeeze_tensor.size()
        output_tensor = torch.mul(input_tensor, fc_out_2.view(a, b, 1, 1))
        if self.stride > 1:
            output_tensor = output_tensor[:, :, ::self.stride, ::self.stride]
        return output_tensor


class FeedForward(nn.Module):
    def __init__(self, dim, hidden_dim, dropout = 0., light=False):
        super().__init__()
        self.net = nn.Sequential(
            lin_layer(light)(dim, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            lin_layer(light)(hidden_dim, dim),
            nn.Dropout(dropout)
        )
    def forward(self, x):
        return self.net(x)


class PosEnc(nn.Module):
    def __init__(self, C, ks, light=False):
        super().__init__()
        fn = torch.empty if light else torch.randn
        self.weight = nn.Parameter(fn(1, C, ks, ks))

    def forward(self, x):
        return  x + self.weight


class Transformer(nn.Module):
    """
    Copied from https://github.com/lucidrains/vit-pytorch/blob/main/vit_pytorch/vit.py

    MIT License

    Copyright (c) 2020 Phil Wang

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

    """
    def __init__(self, dim, dim_out=None, heads=8, dim_head=None, dropout=0., stride=1, light=False):
        super().__init__()
        self.stride = stride
        if dim_head is None:
            dim_head = dim // heads
        inner_dim = dim_head * heads
        self.heads = heads
        self.scale = dim_head ** -0.5

        self.to_qkv = lin_layer(light)(dim, inner_dim * 3, bias=False)
        self.to_out = nn.Sequential(
            lin_layer(light)(inner_dim, dim_out),
            nn.Dropout(dropout)
        )

        self.ln1 = ln_layer(dim, light)
        self.ln2 = ln_layer(dim, light)
        self.ff = FeedForward(dim, dim_out, light=light)
        self.stride = stride

    def forward(self, x, mask=None):
        sz = x.shape
        if len(sz) == 4:
            x = x.reshape(sz[0], sz[1], -1).permute(0, 2, 1)

        assert x.dim() == 3, (x.shape, sz)
        x_in = x
        x = self.ln1(x)

        b, n, _, h = *x.shape, self.heads
        qkv = self.to_qkv(x).chunk(3, dim=-1)

        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=h), qkv)
        dots = torch.einsum('bhid,bhjd->bhij', q, k) * self.scale
        if mask is not None:
            raise NotImplementedError('should not be used for images')

        attn = dots.softmax(dim=-1)

        out = torch.einsum('bhij,bhjd->bhid', attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        out = self.to_out(out)
        # end of MSA
        out = out + x_in  # residual

        out = self.ff(self.ln2(out)) + out  # mlp + residual

        if len(sz) == 4:
            out = out.permute(0, 2, 1).view(sz)  # B,C,H,W
            if self.stride > 1:
                out = out[:, :, ::self.stride, ::self.stride]

        return out


# DARTS License and code below
"""
   Copyright (c) 2018, Hanxiao Liu.
   All rights reserved.

                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS

   APPENDIX: How to apply the Apache License to your work.

      To apply the Apache License to your work, attach the following
      boilerplate notice, with the fields enclosed by brackets "[]"
      replaced with your own identifying information. (Don't include
      the brackets!)  The text should be enclosed in the appropriate
      comment syntax for the file format. We also recommend that a
      file or class name and description of purpose be included on the
      same "printed page" as the copyright notice for easier
      identification within third-party archives.

   Copyright [yyyy] [name of copyright owner]

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""


class ReLUConvBN(nn.Module):

    def __init__(self, C_in, C_out, ks=1, stride=1, padding=0, norm='bn', double=False, light=False):
        super(ReLUConvBN, self).__init__()
        self.stride = stride
        if double:
            conv = [
                conv_layer(light)(C_in, C_in, (1, ks), stride=(1, stride),
                          padding=(0, padding), bias=False),
                conv_layer(light)(C_in, C_out, (ks, 1), stride=(stride, 1),
                          padding=(padding, 0), bias=False)]
        else:
            conv = [conv_layer(light)(C_in, C_out, ks, stride=stride, padding=padding, bias=False)]
        self.op = nn.Sequential(
            nn.ReLU(inplace=False),
            *conv,
            bn_layer(norm, C_out, light))

    def forward(self, x):
        return self.op(x)


class DilConv(nn.Module):
    
    def __init__(self, C_in, C_out, ks, stride, padding, dilation, norm='bn', light=False):
        super(DilConv, self).__init__()
        self.stride = stride

        self.op = nn.Sequential(
            nn.ReLU(inplace=False),
            conv_layer(light)(C_in, C_in, kernel_size=ks, stride=stride, padding=padding, dilation=dilation, groups=C_in, bias=False),
            conv_layer(light)(C_in, C_out, kernel_size=1, padding=0, bias=False),
            bn_layer(norm, C_out, light)
            )

    def forward(self, x):
        return self.op(x)


class SepConv(nn.Module):
    
    def __init__(self, C_in, C_out, ks, stride, padding, norm='bn', light=False):
        super(SepConv, self).__init__()
        self.stride = stride

        self.op = nn.Sequential(
            nn.ReLU(inplace=False),
            conv_layer(light)(C_in, C_in, kernel_size=ks, stride=stride, padding=padding, groups=C_in, bias=False),
            conv_layer(light)(C_in, C_in, kernel_size=1, padding=0, bias=False),
            bn_layer(norm, C_in, light),
            nn.ReLU(inplace=False),
            conv_layer(light)(C_in, C_in, kernel_size=ks, stride=1, padding=padding, groups=C_in, bias=False),
            conv_layer(light)(C_in, C_out, kernel_size=1, padding=0, bias=False),
            bn_layer(norm, C_out, light)
            )

    def forward(self, x):
        return self.op(x)


class Stride(nn.Module):
    def __init__(self, stride):
        super(Stride, self).__init__()
        self.stride = stride

    def forward(self, x):
        if self.stride == 1:
            return x
        return x[:,:,::self.stride,::self.stride]


class Zero(nn.Module):
    def __init__(self, stride):
        super(Zero, self).__init__()
        self.stride = stride

    def forward(self, x):
        if self.stride == 1:
            return x.mul(0.)
        return x[:,:,::self.stride,::self.stride].mul(0.)


class FactorizedReduce(nn.Module):
    def __init__(self, C_in, C_out, norm='bn', stride=2, light=False):
        super(FactorizedReduce, self).__init__()
        assert C_out % 2 == 0
        self.stride = stride
        self.relu = nn.ReLU(inplace=False)
        self.conv_1 = conv_layer(light)(C_in, C_out // 2, 1, stride=stride, padding=0, bias=False)
        self.conv_2 = conv_layer(light)(C_in, C_out // 2, 1, stride=stride, padding=0, bias=False)
        self.bn = bn_layer(norm, C_out, light)

    def forward(self, x):
        x = self.relu(x)
        out = torch.cat([self.conv_1(x), self.conv_2(x[:,:,1:,1:] if self.stride > 1 else x)], dim=1)
        out = self.bn(out)
        return out
