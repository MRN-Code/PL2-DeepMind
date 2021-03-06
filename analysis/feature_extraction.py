"""
Module for feature extraction.
"""

__author__ = "Devon Hjelm"
__copyright__ = "Copyright 2014, Mind Research Network"
__credits__ = ["Devon Hjelm"]
__licence__ = "3-clause BSD"
__email__ = "dhjelm@mrn.org"
__maintainer__ = "Devon Hjelm"

import matplotlib
matplotlib.use("Agg")

import copy
import logging
from matplotlib import pylab as plt
from munkres import Munkres
import numpy as np
from os import path

from pl2mind.datasets.MRI import MRI
from pl2mind.datasets.MRI import MRI_Standard
from pl2mind.datasets.MRI import MRI_Transposed

import pprint

from pylearn2.blocks import Block
from pylearn2.config import yaml_parse
from pylearn2.datasets.transformer_dataset import TransformerDataset
from pylearn2.models.dbm.layer import GaussianVisLayer
from pylearn2.models.dbm import DBM
from pylearn2.models.dbm.dbm import RBM
from pylearn2.models.dbn import DBN
from pylearn2.models.vae import VAE
from pylearn2.utils import sharedX

import networkx as nx

import theano
from theano import tensor as T


logger = logging.getLogger("pl2mind")

try:
    from nice.pylearn2.models.nice import NICE
    import nice.pylearn2.models.mlp as nice_mlp
except ImportError:
    class NICE (object):
        pass
    nice_mlp = None
    logger.warn("NICE not found, so hopefully you're "
                "not trying to load a NICE model.")


class ModelStructure(object):
    def __init__(self, model, dataset):
        self.__dict__.update(locals())
        del self.self

        self.parse_transformers()

    def parse_transformers(self):
        self.transformers = []
        while isinstance(self.dataset, TransformerDataset):
            logger.info("Found transformer of type %s"
                        % type(self.dataset.transformer))
            self.transformers.append(self.dataset.transformer)
            self.dataset = self.dataset.raw
        if isinstance(self.model, DBN):
            self.transformers += self.model.rbms[:-1]

    def transposed(self):
        return isinstance(self.dataset, MRI_Transposed)

    def get_ordered_list(self):
        return self.transformers + [self.model]

    def get_name_dict(self):

        name_dict = {}

        class Namer(object):
            def __init__(self):
                self.d = {}

            def get_index(self, key):
                if not self.d.get(key, False):
                    self.d[key] = 0
                    return str(self.d[key])
                else:
                    self.d[key] += 1
                    return str(self.d[key])

            def __call__(self, model):
                if isinstance(model, RBM):
                    name = "RBM"
                elif isinstance(model, DBN):
                    name = "DBN"
                elif isinstance(model, VAE):
                    name = "VAE"
                elif isinstance(model, NICE):
                    name = "NICE"
                elif isinstance(model, TransformerDataset):
                    name = "transformer"
                else:
                    name = "Unknown"
                return name + self.get_index(name)

        namer = Namer()
        for model in self.transformers + [self.model]:
            name_dict[model] = namer(model)

        return name_dict

    def get_graph(self):
        graph = dict()

        for i, (t1, t2) in enumerate(zip(self.transformers[:-1],
                                         self.transformers[1:])):
            graph["T%d" % i] = (t1, t2)

        if len(self.transformers) == 0:
            graph["D"] = (self.dataset, self.model)
        else:
            graph["D"] = (self.dataset, self.transformers[0])
            graph["T%d" % (len(self.transformers) - 1)] = (
                self.transformers[-1], self.model
            )
        return graph

    def get_named_graph(self):
        graph = self.get_graph()
        named_graph = dict()
        for k, (v1, v2) in graph.iteritems():
            # Hack to get the name out
            n1 = str(type(v1)).split(".")[-1][:-2]
            n2 = str(type(v2)).split(".")[-1][:-2]
            named_graph[k] = (n1, n2)

        return named_graph


class Feature(object):
    def __init__(self, j):
        self.stats = {}
        self.match_indices = {}
        self.relations = {}
        self.id = j


class Features(object):
    def __init__(self, F, X, name="", transposed=False, idx=None, **stats):

        if idx is None:
            idx = range(F.shape[0])
            assert F.shape[0] == X.shape[0], (
                "Shape mismatch: %s vs %s" % (F.shape, X.shape)
            )
        else:
            if isinstance(idx, list):
                idx = idx
            else:
                idx = idx.eval()

        self.name = name
        if transposed:
            self.spatial_maps = F[idx]
            self.activations = X
        else:
            self.spatial_maps = F
            self.activations = X[idx]

        self.f = {}
        for i, j in enumerate(idx):
            self.f[i] = Feature(j)

        self.stat_names = []
        for stat, value in stats.iteritems():
            self.stat_names.append(stat)
            self.load_feature_stats(stat, value.eval())

        self.stats = {}
        self.relations = {}
        self.load_stats()
        self.clean()

    def __getitem__(self, key):
        return self.f[key]

    def load_stats(self):
        sm_means = (np.abs(self.spatial_maps).mean(axis=1) /
                    np.abs(self.spatial_maps).mean())

        sm_maxes = (np.abs(self.spatial_maps).max(axis=1) /
                    np.abs(self.spatial_maps).max())

        self.stats.update(**dict(
            sm_means=np.sort(sm_means).tolist(),
            sm_maxes=np.sort(sm_maxes).tolist()
        ))
        self.load_feature_stats("mean_weight_prop",
                               dict((k, sm_means[k]) for k in self.f.keys()))
        self.load_feature_stats("max_weight_prop",
                               dict((k, sm_maxes[k]) for k in self.f.keys()))

    def load_feature_stats(self, stat_name, value):
        for k in self.f.keys():
            self.f[k].stats[stat_name] = value[k]

    def __call__(self):
        return self.features

    def clean(self):
        remove_idx = []
        for f in self.f.keys():
            if (np.abs(self.spatial_maps[f]).mean()
                / np.abs(self.spatial_maps).mean() < 0.2):
                remove_idx.append(f)
        logger.info("Removing %d features" % len(remove_idx))
        keep_idx = [i for i in range(len(self.f)) if i not in remove_idx]
        new_f = {}
        self.spatial_maps = self.spatial_maps[keep_idx]
        self.activations = self.activations[keep_idx]
        for i, j in enumerate(keep_idx):
            new_f[i] = self.f[j]
        self.f = new_f

    def set_histograms(self, bins=100, tolist=False):
        for k, f in self.f.iteritems():
            sm_bins, sm_edges = np.histogram(self.spatial_maps[k], bins=bins)
            act_bins, act_edges = np.histogram(self.activations[k], bins=bins)
            f.hists = dict()
            f.hists["sm_hists"] = dict(
                bins=sm_bins.tolist() if tolist else sm_bins,
                edges=sm_edges.tolist() if tolist else sm_edges
            )
            f.hists["act_hists"] = dict(
                bins=act_bins.tolist() if tolist else act_bins,
                edges=act_edges.tolist() if tolist else act_edges
            )

    def get_nodes(self):
        nodes = [
            dict(
                name="%d" % f.id
            )
            for f in self.f.values()
        ]
        return nodes

    def get_links(self, stat, absolute_value=True, other=None):
        assert stat in self.stat_names + ["spatial_maps", "activations"]


        if stat == "spatial_maps":
            corrs = np.corrcoef(self.spatial_maps,
                                self.spatial_maps)[len(self.f):, :len(self.f)]

        elif stat == "activations":
            corrs = np.corrcoef(self.spatial_maps,
                                self.spatial_maps)[len(self.f):, :len(self.f)]
        else:
            raise NotImplementedError()

        links = []

        if absolute_value:
            corrs = np.abs(corrs)

        for i in xrange(len(self.f)):
            for j in xrange(i + 1, len(self.f)):
                links.append(
                    dict(
                        source=i,
                        target=j,
                        value=corrs[i, j]
                    )
                )

        return links

    def relate(self, other_features, F):
        relation = F.eval()
        relation = relation / max(abs(relation.min()), relation.max())
        idx = [f.id for f in other_features.f.values()]
        relation = relation[:, idx]
        self.relations[other_features.name] = relation.tolist()
        for k, f in self.f.iteritems():
            f.relations[other_features.name] = relation[k].tolist()


def match_parameters(p, q, method="munkres", discard_misses=False):
    """
    Match two sets of parameters.
    TODO: finish greedy
    """
    logger.info("Matching with method %s" % method)
    assert p.shape[1] == q.shape[1], (
        "Shapes do not match (%s vs %s)" % (p.shape, q.shape)
    )

    match_size = min(p.shape[0], q.shape[0])
    corrs = np.corrcoef(p, q)[match_size:, :match_size]
    corrs[np.isnan(corrs)] = 0

    if method == "munkres":
        m = Munkres()
        cl = 1 - np.abs(corrs)
        if (cl.shape[0] > cl.shape[1]):
            indices = m.compute(cl.T)
        else:
            indices = m.compute(cl)
            indices = [(i[1], i[0]) for i in indices]

    elif method == "greedy":
        q_idx = []
        raise NotImplementedError("Greedy not supported yet.")
        for c in range(q.shape[0]):
            idx = corrs[c, :].argmax()
            q_idx.append(idx)
            corrs[:,idx] = 0

    else:
        raise NotImplementedError("%s matching not supported" % method)

    return indices

def resolve_dataset(model, dataset_root=None, **kwargs):
    """
    Resolves the full dataset from the model.
    In most cases we want to use the full unshuffled dataset for analysis,
    so we change the dataset class to use it here.
    """

    logger.info("Resolving full dataset from training set.")
    dataset_yaml = model.dataset_yaml_src
    if "MRI_Standard" in dataset_yaml:
        dataset_yaml = dataset_yaml.replace("\"train\"", "\"full\"")

    if dataset_root is not None:
        logger.warn("Hacked transformer dataset dataset_root in. "
                     "Need to parse yaml properly. If you encounter "
                     "problems with the dataset, this may be the reason.")
        dataset_yaml = dataset_yaml.replace("dataset_name", "dataset_root: %s, "
                                            "dataset_name" % dataset_root)
    logger.info("Final yaml is: \n%s" % pprint.pformat(dataset_yaml))
    dataset = yaml_parse.load(dataset_yaml)
    return dataset

def extract_features(model, dataset_root=None, zscore=False, max_features=100,
                     multiply_variance=False, **kwargs):
    """
    Extracts the features given a number of model types.

    Included are special methods for VAE and NICE.
    Also if the data is transposed, the appropriate matrix multiplication of
    data x features is used.

    Parameters
    ----------
    model: pylearn2 Model class
        Model from which to extract features.
    dataset: pylearn2 Dataset class.
        Dataset to process transposed features.
    max_features: int, optional
        maximum number of features to process.

    Returns
    -------
    features: array_like.
    """

    logger.info("Extracting dataset")
    dataset = resolve_dataset(model, dataset_root)

    ms = ModelStructure(model, dataset)
    models = ms.get_ordered_list()
    name_dict = ms.get_name_dict()

    logger.info("Getting activations for model of type %s"
                % (type(model)))
    data = ms.dataset.get_design_matrix()
    X = sharedX(data)

    feature_dict = {"dataset": dataset}
    for i, model in enumerate(models):
        logger.info("Passing data through %s" % model)
        F, stats = get_features(model)

        downward_relations = {}
        for j in range(i - 1, -1, -1):
            model_below = models[j]
            features_below = feature_dict[name_dict[model_below]]
            downward_relations[name_dict[model_below]] = F
            F = downward_message(F, model_below)
        X = upward_message(X, model)

        if ms.transposed():
            sms = X.T.eval()
            acts = F.eval()
        else:
            sms = F.eval()
            acts = X.T.eval()

        if dataset.variance_map is not None and multiply_variance:
            logger.info("Multiplying by variance map")
            axis = dataset.variance_map[0]
            vm = dataset.variance_map[1]
            if ms.transposed():
                axis = (axis + 1) % 2
            if axis == 0:
                sms = sms * vm
            elif axis == 1:
                sms = (sms.T * vm).T
            else:
                raise ValueError("Axis %s for variance map not supported"
                                 % axis)
        f = Features(sms, acts, transposed=ms.transposed(),
                     name=name_dict[model], **stats)

        for j in range(i - 1, -1, -1):
            model_below = models[j]
            f.relate(feature_dict[name_dict[model_below]],
                     downward_relations[name_dict[model_below]])

        feature_dict[name_dict[model]] = f

    return feature_dict

def get_features(model, max_features=100):
    """
    Form the original features in the model representation.

    Parameters
    ----------
    model: pylearn2 Model
        The model.
    max_features: int
        The maximum number of features to process.

    Returns
    -------
    features, stats
    """

    def make_vec(i, V):
        vec, updates = theano.scan(
            fn=lambda x, j: T.switch(T.eq(i, j), x, 0),
            sequences=[V, theano.tensor.arange(V.shape[0])],
            outputs_info=[None])
        return vec

    if isinstance(model, VAE):
        logger.info("Getting features for VAE model")
        means = model.prior.prior_mu
        sigmas = T.exp(model.prior.log_prior_sigma)

        idx = sigmas.argsort()[:max_features]

        means_matrix, updates = theano.scan(
            fn=lambda x: x,
            non_sequences=[means[idx]],
            n_steps=idx.shape[0])

        sigmas_matrix, updates = theano.scan(
            make_vec,
            sequences=[idx],
            non_sequences=[sigmas]
        )

        theta0 = model.decode_theta(means_matrix)
        mu0, log_sigma0 = theta0

        theta1 = model.decode_theta(means_matrix + 2 * sigmas_matrix)
        mu1, log_sigma1 = theta1

        features = 1 - (0.5 * (1 + T.erf((mu0 - mu1) / (
            T.exp(log_sigma1) * sqrt(2)))))
        stats = dict(m=means[idx], s=sigmas[idx], idx=idx)

    elif isinstance(model, NICE):
        logger.info("Getting features for NICE model")
        top_layer = model.encoder.layers[-1]
        if isinstance(top_layer, nice_mlp.Homothety):
            S = top_layer.D
            sigmas = T.exp(-S)
        elif isinstance(top_layer, nice_mlp.SigmaScaling):
            sigmas = top_layer.S

        top_layer = model.encoder.layers[-1]

        idx = sigmas.argsort()[:max_features]

        sigmas_matrix, updates = theano.scan(
            make_vec,
            sequences=[idx],
            non_sequences=[sigmas]
        )

        means_matrix = T.zeros_like(sigmas_matrix)
        mean_features = model.encoder.inv_fprop(means_matrix)

        features = (model.encoder.inv_fprop(2 * sigmas_matrix) - mean_features)
        stats = dict(s=sigmas[idx], idx=idx)

    elif isinstance(model, RBM):
        features = model.hidden_layer.transformer.get_params()[0].T
        #if isinstance(model.visible_layer, GaussianVisLayer):
        #    X = T.eye(features.shape[0], model.visible_layer.nvis)
        #    X -= model.visible_layer.mu
        #    features = X.T.dot(features)

        stats = dict()

    elif isinstance(model, DBN):
        features, _ = get_features(model.top_rbm,
                                   max_features=max_features)
        stats = dict()

    else:
        raise NotImplementedError("No feature extraction for mode %s"
                                  % type(model))

    return (features, stats)

def downward_message(Y, model):
    """
    WRITEME
    """

    if isinstance(model, NICE):
        x = model.encoder.inv_prop(Y)

    elif isinstance(model, VAE):
        theta = model.decode_theta(Y)
        mu, log_sigma = theta
        x = mu

    elif isinstance(model, RBM):
        hidden_layer = model.hidden_layer
        x = hidden_layer.downward_message(Y)

    elif isinstance(model, DBN):
        X = sharedX(np.identity(model.rbms[0].visible_layer.nvis))
        X = model.feed_forward(X)
        x = X.dot(Y).T

    else:
        raise NotImplementedError()

    return x

def upward_message(X, model):
    """
    Get latent variable activations given a dataset.

    Parameters
    ----------
    model: pylearn2.Model
        Model from which to get activations.
    dataset: pylearn2.datasets.DenseDesignMatrix
        Dataset from which to generate activations.

    Returns
    -------
    activations: numpy array-like
    """

    if isinstance(model, NICE):
        y = model.encode(X)

    elif isinstance(model, VAE):
        epsilon = model.sample_from_epsilon((X.shape[0], model.nhid))
        epsilon *= 0
        phi = model.encode_phi(X)
        y = model.sample_from_q_z_given_x(epsilon=epsilon, phi=phi)

    elif isinstance(model, RBM):
        y = model(X)

    elif isinstance(model, DBN):
        y = model.top_rbm(X)

    elif isinstance(model, Block):
        y = model(X)

    else:
        raise NotImplementedError("Cannot get activations for model of type %r."
                                  " Needs to be implemented"
                                  % type(model))

    return y

def save_nice_spectrum(model, out_dir):
    """
    Generates the NICE spectrum from a NICE model.
    """
    logger.info("Getting NICE spectrum")
    if not isinstance(model, NICE):
        raise NotImplementedError("No spectrum analysis available for %r"
                                  % type(model))

    top_layer = model.encoder.layers[-1]
    if isinstance(top_layer, nice_mlp.Homothety):
        S = top_layer.D.get_value()
        spectrum = np.exp(-S)
    elif isinstance(top_layer, nice_mlp.SigmaScaling):
        spectrum = top_layer.S.get_value()
    spectrum = -np.sort(-spectrum)
    f = plt.figure()
    plt.plot(spectrum)
    f.savefig(path.join(out_dir, "nice_spectrum.pdf"))

def get_convolved_activations(model, dataset_root=None,
                              x_size=20, x_stride=20,
                              y_size=20, y_stride=20,
                              z_size=20, z_stride=20):
    """
    Get convolved activations for model.
    """

    assert isinstance(model, RBM)
    #model.call_method = "MUL"

    dataset = resolve_dataset(model, dataset_root=dataset_root)
    X = sharedX(dataset.get_topological_view(dataset.X))

    def local_filter(i, sample):
        num_x = sample.shape[0] // x_stride
        num_y = sample.shape[1] // y_stride
        num_z = sample.shape[2] // z_stride

        x = (i / (num_y * num_z)) * x_stride
        y = ((i / num_z) % num_y) * y_stride
        z = (i % num_z) * z_stride
        zeros = T.zeros_like(sample)
        rval = T.set_subtensor(zeros[x: x + x_size,
                                     y: y + y_size,
                                     z: z + z_size],
                               sample[x: x + x_size,
                                     y: y + y_size,
                                     z: z + z_size])
        return rval

    def conv_data(sample,):
        num_x = sample.shape[0] // x_stride
        num_y = sample.shape[1] // y_stride
        num_z = sample.shape[2] // z_stride
        total_num = num_x * num_y * num_z
        topo, update = theano.scan(local_filter,
                                   sequences=[theano.tensor.arange(total_num)],
                                   non_sequences=[sample])
        data = dataset.view_converter.tv_to_dm_theano(topo)
        return data

    subjects, updates = theano.scan(conv_data, sequences=[X])

    def get_activations(x):
        y = upward_message(x, model)
        return y

    activations, updates = theano.scan(get_activations,
                                       sequences=[subjects])

    return activations.eval()