# coding=utf-8
# Copyright 2020 The Tensor2Robot Authors.
#
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

# Lint as: python3
"""Predictor that relies on TF2.x SavedModels."""

import os
import time
from typing import Dict, Optional, Text
from absl import logging
import gin
import numpy as np

from tensor2robot.predictors import abstract_predictor
from tensor2robot.utils import tensorspec_utils
import tensorflow.compat.v2 as tf

_BUSY_WAITING_SLEEP_TIME_IN_SECS = 10


class SavedModelPredictorBase(abstract_predictor.AbstractPredictor):
  """Base SavedModel predictor.

  See implementations for TF1 and TF2 below.
  """

  def __init__(self, saved_model_path, timeout = 600):
    """Creates an instance.

    Args:
      saved_model_path: A path to a directory containing the saved_model.
      timeout: (defaults to 600 seconds) If no checkpoint has been found after
        timeout seconds restore fails.
    """
    super(SavedModelPredictorBase, self).__init__()
    self._saved_model_path = saved_model_path
    self._timeout = timeout
    self._model = None

    self._feature_spec = None  # type: tensorspec_utils.TensorSpecStruct
    self._label_spec = None

  def predict(self, features):
    """Predicts based on feature input using the loaded model.

    Args:
      features: A dict containing the features used for predictions.

    Returns:
      The result of the queried model predictions.
    """
    self.assert_is_loaded()

    def _maybe_expand_dims(f, spec):
      if list(f.shape) == spec.shape.as_list():
        return np.expand_dims(f, 0)
      return f

    expanded_features = tf.nest.map_structure(_maybe_expand_dims, features,
                                              self.get_feature_specification())
    predictions = self._model.predict(expanded_features)

    return predictions

  def get_feature_specification(self):
    """Exposes the required input features for evaluation of the model."""
    self.assert_is_loaded()
    return self._feature_spec

  def get_label_specification(
      self):
    """Exposes the optional labels for evaluation of the model."""
    self.assert_is_loaded()
    return self._label_spec

  def restore(self):
    """Restores the model parameters from the latest available data."""

    # Get the expected assets filename.
    t2r_assets_dir = os.path.join(self._saved_model_path,
                                  tensorspec_utils.EXTRA_ASSETS_DIRECTORY)
    t2r_assets_filename = os.path.join(t2r_assets_dir,
                                       tensorspec_utils.T2R_ASSETS_FILENAME)

    start_time = time.time()
    while time.time() - start_time < self._timeout:
      # Check for the assets.extra/t2r_assets.pbtxt file which is materialized
      # last. Otherwise we should check for saved_model.pb
      if tf.io.gfile.exists(t2r_assets_filename):
        break

      logging.info('Waiting for a saved model to become available at %s.',
                   self._saved_model_dir)
      time.sleep(_BUSY_WAITING_SLEEP_TIME_IN_SECS)
    else:
      logging.warning('No saved_model found after %s seconds.',
                      str(self._timeout))
      return False

    # Loading assets for features and labels.
    t2r_assets_file_path = os.path.join(self._saved_model_path,
                                        tensorspec_utils.EXTRA_ASSETS_DIRECTORY,
                                        tensorspec_utils.T2R_ASSETS_FILENAME)
    t2r_assets = tensorspec_utils.load_t2r_assets_to_file(t2r_assets_file_path)

    self._feature_spec = tensorspec_utils.TensorSpecStruct.from_proto(
        t2r_assets.feature_spec)  # pytype: disable=wrong-arg-types
    self._label_spec = tensorspec_utils.TensorSpecStruct.from_proto(
        t2r_assets.label_spec)  # pytype: disable=wrong-arg-types

    self._model = tf.saved_model.load(self._saved_model_path)
    return True

  def init_randomly(self):
    """Initializes model parameters from with random values."""
    raise ValueError('Random initialization is not supported '
                     'for SavedModelPredictor.')

  def close(self):
    """Closes all open handles used throughout model evaluation.

    Raises a ValueError if the predictor has not been restored yet.
    """
    self.assert_is_loaded()

  def assert_is_loaded(self):
    if self._model is None:
      raise ValueError('The predictor has not yet been successfully restored.')

  @property
  def model_version(self):
    """The version of the model currently in use."""
    self.assert_is_loaded()
    return int(os.path.basename(self._saved_model_path))

  @property
  def global_step(self):
    """The global step of the model currently in use."""
    self.assert_is_loaded()
    return self._model.global_step()

  @property
  def model_path(self):
    """The path of the model currently in use."""
    self.assert_is_loaded()
    return self._saved_model_path


@gin.configurable
class SavedModelTF2Predictor(SavedModelPredictorBase):
  """SavedModelTF2Predictor compatible with TF2.x and Eager execution.

  Note that this is doing inference on a concrete function loaded from the saved
  model so although it can be used in Eager mode this is not expected to have
  any performance penalties when compared to the graph TF1.x version below.
  """

  def predict(self, features):
    predictions = super(SavedModelTF2Predictor, self).predict(features)
    return tf.nest.map_structure(lambda t: t.numpy(), predictions)

  @property
  def global_step(self):
    """The global step of the model currently in use."""
    self.assert_is_loaded()
    return self._model.global_step().numpy()


@gin.configurable
class SavedModelTF1Predictor(SavedModelPredictorBase):
  """SavedModel predictor that works with graph mode and TF1.x.

  Creates it's own graph and session to execute model predictions.
  """

  def __init__(self,
               saved_model_path,
               timeout = 600,
               tf_config = None):
    """Creates an instance.

    Args:
      saved_model_path: A path to a directory containing the saved_model.
      timeout: (defaults to 600 seconds) If no checkpoint has been found after
        timeout seconds restore fails.
      tf_config: The tf.ConfigProto used to configure the TensorFlow session.
    """
    super(SavedModelTF1Predictor, self).__init__(saved_model_path, timeout)
    self._tf_config = tf_config
    self._graph = tf.compat.v1.Graph()
    self._session = tf.compat.v1.Session(graph=self._graph, config=tf_config)
    self._predictions = None
    self._features = None

  def predict(self, features):
    self.assert_is_loaded()

    # Forcing both session and graph to be defaults here to force a graph
    # context if this ever gets used during Eager execution. Makes testing
    # easier too.
    with self._session.as_default(), self._graph.as_default():
      return self._session.run(
          self._predictions,
          tensorspec_utils.map_feed_dict(self._features, features))

  def restore(self):
    # Forcing both session and graph to be defaults here to force a graph
    # context if this ever gets used during Eager execution. Makes testing
    # easier too.
    with self._session.as_default(), self._graph.as_default():
      if not super(SavedModelTF1Predictor, self).restore():
        return False

      self._features = tensorspec_utils.make_placeholders(
          self._feature_spec, batch_size=None)
      self._predictions = super(SavedModelTF1Predictor,
                                self).predict(self._features)
      # After loading the model we need to make sure we initialize the
      # variables.
      variables = (
          tf.compat.v1.global_variables() + tf.compat.v1.local_variables())
      self._session.run(tf.compat.v1.variables_initializer(variables))

  @property
  def global_step(self):
    """The global step of the model currently in use."""
    self.assert_is_loaded()
    return self._session.run(super(SavedModelTF1Predictor, self).global_step())
