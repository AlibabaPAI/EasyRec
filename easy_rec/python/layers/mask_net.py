# -*- encoding:utf-8 -*-
# Copyright (c) Alibaba, Inc. and its affiliates.
import tensorflow as tf

from easy_rec.python.layers import dnn
from easy_rec.python.layers.common_layers import layer_norm

if tf.__version__ >= '2.0':
  tf = tf.compat.v1


class MaskBlock(object):

  def __init__(self, mask_block_config, name='mask_block', reuse=None):
    self.mask_block_config = mask_block_config
    self.name = name
    self.reuse = reuse

  def __call__(self, net, mask_input):
    mask_input_dim = int(mask_input.shape[-1])
    if self.mask_block_config.HasField('reduction_factor'):
      aggregation_size = int(mask_input_dim *
                             self.mask_block_config.reduction_factor)
    elif self.mask_block_config.HasField('aggregation_size') is not None:
      aggregation_size = self.mask_block_config.aggregation_size
    else:
      raise ValueError(
          'Need one of reduction factor or aggregation size for MaskBlock.')

    if self.mask_block_config.input_layer_norm:
      input_name = net.name.replace(':', '_')
      net = layer_norm(net, reuse=tf.AUTO_REUSE, name='ln_' + input_name)

    # initializer = tf.initializers.variance_scaling()
    initializer = tf.glorot_uniform_initializer()
    mask = tf.layers.dense(
        mask_input,
        aggregation_size,
        activation=tf.nn.relu,
        kernel_initializer=initializer,
        name='%s/hidden' % self.name,
        reuse=self.reuse)
    mask = tf.layers.dense(
        mask, net.shape[-1], name='%s/mask' % self.name, reuse=self.reuse)
    masked_net = net * mask

    output_size = self.mask_block_config.output_size
    hidden = tf.layers.dense(
        masked_net,
        output_size,
        use_bias=False,
        name='%s/output' % self.name,
        reuse=self.reuse)
    ln_hidden = layer_norm(
        hidden, name='%s/ln_output' % self.name, reuse=self.reuse)
    return tf.nn.relu(ln_hidden)


class MaskNet(object):

  def __init__(self, mask_net_config, name='mask_net', reuse=None):
    """MaskNet: Introducing Feature-Wise Multiplication to CTR Ranking Models by Instance-Guided Mask.

    Refer: https://arxiv.org/pdf/2102.07619.pdf
    """
    self.mask_net_config = mask_net_config
    self.name = name
    self.reuse = reuse

  def __call__(self, inputs, is_training, l2_reg=None):
    conf = self.mask_net_config
    if conf.use_parallel:
      mask_outputs = []
      for i, block_conf in enumerate(self.mask_net_config.mask_blocks):
        mask_layer = MaskBlock(
            block_conf, name='%s/block_%d' % (self.name, i), reuse=self.reuse)
        mask_outputs.append(mask_layer(mask_input=inputs, net=inputs))
      all_mask_outputs = tf.concat(mask_outputs, axis=1)

      if conf.HasField('mlp'):
        mlp = dnn.DNN(
            conf.mlp,
            l2_reg,
            name='%s/mlp' % self.name,
            is_training=is_training,
            reuse=self.reuse)
        output = mlp(all_mask_outputs)
      else:
        output = all_mask_outputs
      return output
    else:
      net = inputs
      for i, block_conf in enumerate(self.mask_net_config.mask_blocks):
        mask_layer = MaskBlock(
            block_conf, name='%s/block_%d' % (self.name, i), reuse=self.reuse)
        net = mask_layer(net=net, mask_input=inputs)

      if conf.HasField('mlp'):
        mlp = dnn.DNN(
            conf.mlp,
            l2_reg,
            name='%s/mlp' % self.name,
            is_training=is_training,
            reuse=self.reuse)
        output = mlp(net)
      else:
        output = net
      return output
