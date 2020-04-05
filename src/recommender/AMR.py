"""
Created on April 4, 2020
Tensorflow 2.1.0 implementation of APR.
@author Felice Antonio Merra (felice.merra@poliba.it)
"""
import numpy as np
from time import time
from recommender.Evaluator import Evaluator
import os
import logging

from util.read import find_checkpoint

np.random.seed(0)
logging.disable(logging.WARNING)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

import tensorflow as tf
from recommender.RecommenderModel import RecommenderModel


class AMR(RecommenderModel):

    def __init__(self, data, path_output_rec_result, path_output_rec_weight, args):
        """
        Create a AMR instance.
        (see https://doi.org/10.1145/3209978.3209981 for details about the algorithm design choices)
        :param data: data loader object
        :param path_output_rec_result: path to the directory rec. results
        :param path_output_rec_weight: path to the directory rec. model parameters
        :param args: parameters
        """
        super(AMR, self).__init__(data, path_output_rec_result, path_output_rec_weight, args.rec)
        self.embedding_size = args.embed_size
        self.learning_rate = args.lr
        self.reg = args.reg
        self.eps = args.eps
        self.epochs = args.epochs
        self.batch_size = args.batch_size
        self.verbose = args.verbose
        self.restore_epochs = args.restore_epochs
        self.evaluator = Evaluator(self, data, args.k)
        self.adv_type = args.adv_type
        self.adv_reg = args.adv_reg

        # Initialize Model Parameters
        self.embedding_P = tf.Variable(
            tf.random.truncated_normal(shape=[self.num_users, self.embedding_size], mean=0.0, stddev=0.01),
            name='embedding_P', dtype=tf.dtypes.float32)  # (users, embedding_size)
        self.embedding_Q = tf.Variable(
            tf.random.truncated_normal(shape=[self.num_items, self.embedding_size], mean=0.0, stddev=0.01),
            name='embedding_Q', dtype=tf.dtypes.float32)  # (items, embedding_size)
        self.h = tf.constant(1.0, tf.float32, [self.embedding_size, 1], name="h")

        # Method to create delta locations for the adv. perturbations
        self.create_delta()

        self.optimizer = tf.keras.optimizers.Adagrad(learning_rate=self.learning_rate)

    def create_delta(self):
        # These variable are used only during attack phase
        self.delta_P = tf.Variable(tf.zeros(shape=[self.num_users, self.embedding_size]), dtype=tf.float32,
                                   trainable=False)
        self.delta_Q = tf.Variable(tf.zeros(shape=[self.num_items, self.embedding_size]), dtype=tf.float32,
                                   trainable=False)

    def get_inference(self, user_input, item_input_pos):
        """
        generate prediciton  matric with respect to passed users' and items indices
        :param user_input: user indices
        :param item_input_pos: item indices
        :return:
        """
        self.embedding_p = tf.reduce_sum(tf.nn.embedding_lookup(self.embedding_P + self.delta_P, user_input), 1)
        self.embedding_q = tf.reduce_sum(tf.nn.embedding_lookup(self.embedding_Q + self.delta_Q, item_input_pos), 1)

        return tf.matmul(self.embedding_p * self.embedding_q,
                         self.h), self.embedding_p, self.embedding_q  # (b, embedding_size) * (embedding_size, 1)

    def fgsm_perturbation(self, user_input, item_input_pos, item_input_neg, batch_idx=0):
        """
        Evaluate Adversarial Perturbation with FGSM-like Approach
        :param user_input:
        :param item_input_pos:
        :param item_input_neg:
        :param batch_idx:
        :return:
        """
        with tf.GradientTape() as tape_adv:
            tape_adv.watch([self.embedding_P, self.embedding_Q])
            # Clean Inference
            output_pos, embed_p_pos, embed_q_pos = self.get_inference(user_input[batch_idx],
                                                                      item_input_pos[batch_idx])
            output_neg, embed_p_neg, embed_q_neg = self.get_inference(user_input[batch_idx],
                                                                      item_input_neg[batch_idx])
            result = tf.clip_by_value(output_pos - output_neg, -80.0, 1e8)
            loss = tf.reduce_sum(tf.nn.softplus(-result))
            loss += self.reg * tf.reduce_mean(
                tf.square(embed_p_pos) + tf.square(embed_q_pos) + tf.square(embed_q_neg))

        grad_P, grad_Q = tape_adv.gradient(loss, [self.embedding_P, self.embedding_Q])
        grad_P, grad_Q = tf.stop_gradient(grad_P), tf.stop_gradient(grad_Q)
        self.delta_P = tf.nn.l2_normalize(grad_P, 1) * self.eps
        self.delta_Q = tf.nn.l2_normalize(grad_Q, 1) * self.eps

    def get_full_inference(self):
        """
        Get Full Predictions useful for Full Store of Predictions
        :return: The matrix of predicted values.
        """
        return tf.matmul(self.embedding_P + self.delta_P, tf.transpose(self.embedding_Q + self.delta_Q))

    def _train_step(self, batches):
        """
        Apply a single training step (acroos all batched in the dataset).
        :param batches: set of batches used fr the training
        :return:
        """
        user_input, item_input_pos, item_input_neg = batches

        for batch_idx in range(len(user_input)):
            with tf.GradientTape() as t:
                t.watch([self.embedding_P, self.embedding_Q])

                # Clean Inference
                self.output_pos, embed_p_pos, embed_q_pos = self.get_inference(user_input[batch_idx],
                                                                               item_input_pos[batch_idx])
                self.output_neg, embed_p_neg, embed_q_neg = self.get_inference(user_input[batch_idx],
                                                                               item_input_neg[batch_idx])
                self.result = tf.clip_by_value(self.output_pos - self.output_neg, -80.0, 1e8)
                self.loss = tf.reduce_sum(tf.nn.softplus(-self.result))

                # Regularization Component
                self.reg_loss = self.reg * tf.reduce_mean(
                    tf.square(embed_p_pos) + tf.square(embed_q_pos) + tf.square(embed_q_neg))

                self.fgsm_perturbation(user_input, item_input_pos, item_input_neg, batch_idx)

                # Adversarial Inference
                self.output_pos_adver, _, _ = self.get_inference(user_input[batch_idx], item_input_pos[batch_idx])
                self.output_neg_adver, _, _ = self.get_inference(user_input[batch_idx], item_input_neg[batch_idx])

                self.result_adver = tf.clip_by_value(self.output_pos_adver - self.output_neg_adver, -80.0, 1e8)
                self.loss_adver = tf.reduce_sum(tf.nn.softplus(-self.result_adver))

                # Loss to be optimized
                self.loss_opt = self.loss + self.adv_reg * self.loss_adver + self.reg_loss

            gradients = t.gradient(self.loss_opt, [self.embedding_P, self.embedding_Q])
            self.optimizer.apply_gradients(zip(gradients, [self.embedding_P, self.embedding_Q]))
            # Restore Deltas
            self.create_delta()

    def train(self):

        saver_ckpt = tf.train.Checkpoint(optimizer=self.optimizer, model=self)

        if not self.restore():
            self.restore_epochs = 1
            print("Training from scratch...")

        for epoch in range(self.restore_epochs, self.epochs + 1):
            batches = self.data.shuffle(self.batch_size)
            self._train_step(batches)
            self.evaluator.eval()

            if epoch % self.verbose == 0 or epoch == 1:
                saver_ckpt.save('{0}/weights-{1}'.format(self.path_output_rec_weight, epoch))

        self.evaluator.store_recommendation()

    def restore(self):
        saver_ckpt = tf.train.Checkpoint(optimizer=self.optimizer, model=self)
        if self.restore_epochs > 1:
            try:
                checkpoint_file = find_checkpoint(self.path_output_rec_weight, self.restore_epochs, self.rec)
                saver_ckpt.restore(checkpoint_file)
                print("Model correctly Restored at Epoch: {0}".format(self.restore_epochs))
                return True
            except Exception as ex:
                print("Error in model restoring operation! {0}".format(ex.message))
        else:
            print("Restore Epochs Not Specified")
        return False

    def attack_full_fgsm(self, attack_eps, attack_name=""):
        """
        Create FGSM ATTACK
        :param attack_eps:
        :param attack_name:
        :return:
        """
        # Set eps perturbation (budget)
        self.eps = attack_eps
        user_input, item_input_pos, item_input_neg = self.data.shuffle(len(self.data._user_input))

        # Calculate Adversarial Perturbations
        self.fgsm_perturbation(user_input, item_input_pos, item_input_neg)

        self.evaluator.eval()

        self.evaluator.store_recommendation(attack_name)
        print('{0} - Completed!'.format(attack_name))
