from laplace.baselaplace import FullLaplace
import torch
from torch.distributions import MultivariateNormal
from torch.nn.utils import vector_to_parameters
import copy
from nngeometry.metrics import FIM
from nngeometry.object import PVector, PMatDense, PMatKFAC, PMatEKFAC, PMatLowRank
from nngeometry.layercollection import LayerCollection
import time
from utils import geodesic


# see https://pytorch.org/docs/stable/_modules/torch/nn/utils/convert_parameters.html
def get_converter_functions(model):
    total_numels = 0
    numels = dict()
    shapes = dict()
    for name, param in dict(model.named_parameters()).items():
        numels[name] = param.numel()
        shapes[name] = param.size()
        total_numels += param.numel()

    def param_shape_to_vector(params):
        vec = []
        for param in params.values():
            vec.append(param.view(-1))
        return torch.cat(vec)

    def params_shape_to_vectors(params):
        vecs = []
        for param in params.values():
            vecs.append(param.view((param.shape[0], -1)))
        return torch.cat(vecs, dim=1)

    def vector_to_param_shape(vector):
        params = dict()
        count = 0
        for name in numels.keys():
            new_count = count + numels[name]
            params[name] = vector[count:new_count].view(shapes[name])
            count = new_count
        return params

    return (
        total_numels,
        param_shape_to_vector,
        params_shape_to_vectors,
        vector_to_param_shape,
    )


class FisherExpLaplace(FullLaplace):
    def __init__(
        self,
        model,
        likelihood,
        sigma_noise=1.0,
        prior_precision=1.0,
        prior_mean=0.0,
        temperature=1.0,
        enable_backprop=False,
        backend=None,
        backend_kwargs=None,
        X_train=None,
        y_train=None,
        logger=None,
        train_loader=None,
        representation="dense",
    ):
        super().__init__(
            model,
            likelihood,
            sigma_noise,
            prior_precision,
            prior_mean,
            temperature,
            enable_backprop,
            backend,
            backend_kwargs,
        )
        self.X_train = X_train
        self.y_train = y_train
        (
            dim,
            param_shape_to_vector,
            params_shape_to_vectors,
            vector_to_param_shape,
        ) = get_converter_functions(self.model)
        self.param_shape_to_vector = param_shape_to_vector
        self.params_shape_to_vectors = params_shape_to_vectors
        self.vector_to_param_shape = vector_to_param_shape
        self.logger = logger
        self.dim = dim
        self.layer_collection = LayerCollection.from_model(self.model)
        self.temp_model = copy.deepcopy(self.model)
        self.train_loader = train_loader
        if representation == "dense":
            self.representation = PMatDense
        elif representation == "kfac":
            self.representation = PMatKFAC
        elif representation == "ekfac":
            self.representation = PMatEKFAC
        elif representation == "lowrank":
            self.representation = PMatLowRank
        else:
            raise NotImplementedError

    def get_regression_quantity(self, params, data_point, vec_v):
        grad, hvp = torch.func.jvp(
            lambda params: (
                torch.func.jacrev(torch.func.functional_call, argnums=1)(
                    self.model,
                    params,
                    data_point,
                ),
            ),
            (params,),
            (self.vector_to_param_shape(vec_v),),
        )

        return (
            self.param_shape_to_vector(grad[0])
            * torch.dot(self.param_shape_to_vector(hvp[0]), vec_v)
            / torch.square(self.sigma_noise)
        )

    def get_classification_quantity(self, params, data_point, vec_v):
        y = torch.func.functional_call(self.classification_model, params, data_point)
        grad, hvp = torch.func.jvp(
            lambda params: (
                torch.func.jacrev(torch.func.functional_call, argnums=1)(
                    self.classification_model,
                    params,
                    data_point,
                ),
            ),
            (params,),
            (self.vector_to_param_shape(vec_v),),
        )
        vecs_grad = self.params_shape_to_vectors(grad[0])
        vecs_hvp = self.params_shape_to_vectors(hvp[0])
        return (
            y,
            vecs_grad,
            torch.einsum("i,ij,i->j", 1.0 / y, vecs_grad, vecs_hvp @ vec_v)
            - torch.einsum(
                "i,ij,i->j",
                0.5 / torch.square(y),
                vecs_grad,
                torch.square(vecs_grad @ vec_v),
            ),
        )

    def regression_christoffel_fn(self, vec_params, vec_v):
        vec_params = torch.as_tensor(
            vec_params, dtype=torch.float32, device=self._device
        )
        params = self.vector_to_param_shape(vec_params)
        vec_v = torch.as_tensor(vec_v, dtype=torch.float32, device=self._device)
        vector_to_parameters(vec_params, self.temp_model.parameters())
        nngeometry_metric = FIM(
            model=self.temp_model,
            loader=self.train_loader,
            representation=self.representation,
            n_output=1,
            variant="regression",
        )
        quantity = torch.sum(
            torch.vmap(self.get_regression_quantity, in_dims=(None, 0, None))(
                params, self.X_train, vec_v
            ),
            dim=0,
        )
        result = nngeometry_metric.solve(
            PVector(self.layer_collection, quantity / float(len(self.y_train))),
            regul=self.prior_precision
            * torch.square(self.sigma_noise)
            / float(len(self.y_train)),
        ).get_flat_representation() * torch.square(self.sigma_noise)
        return result.detach().cpu().numpy()

    def classification_christoffel_fn(self, vec_params, vec_v):
        vec_params = torch.as_tensor(
            vec_params, dtype=torch.float32, device=self._device
        )
        params = self.vector_to_param_shape(vec_params)
        vec_v = torch.as_tensor(vec_v, dtype=torch.float32, device=self._device)
        vector_to_parameters(vec_params, self.classification_model)
        nngeometry_metric = FIM(
            model=self.classification_model,
            loader=self.train_loader,
            representation=self.representation,
            n_output=2,
            variant="classif_logits",
        )
        quantity = torch.sum(
            torch.vmap(self.get_classification_quantity, in_dims=(None, 0, None))(
                params, self.X_train, vec_v
            ),
            dim=0,
        )
        result = nngeometry_metric.solve(
            PVector(
                LayerCollection.from_model(self.classification_model),
                quantity / float(len(self.y_train)),
            ),
            regul=self.prior_precision / float(len(self.y_train)),
        ).get_flat_representation()
        return result.detach().cpu().numpy()

    def _nn_predictive_samples(self, X, n_samples=100):
        fs = list()
        param_samples, num_evals, times = self.sample(n_samples)
        for sample in param_samples:
            vector_to_parameters(sample, self.model.parameters())
            f = self.model(X.to(self._device))
            fs.append(f.detach() if not self.enable_backprop else f)
        vector_to_parameters(self.mean, self.model.parameters())
        fs = torch.stack(fs)
        if self.likelihood == "classification":
            fs = torch.softmax(fs, dim=-1)
        return param_samples, fs, num_evals, times

    def __call__(
        self,
        x,
        n_samples=100,
    ):
        param_samples, samples, num_evals, times = self._nn_predictive_samples(
            x, n_samples
        )
        if self.likelihood == "regression":
            return (
                param_samples,
                samples.mean(dim=0),
                samples.var(dim=0),
                num_evals,
                times,
            )
        return param_samples, samples.mean(dim=0), num_evals, times

    def sample(self, n_samples=100):
        dist = MultivariateNormal(
            loc=torch.zeros_like(self.mean), scale_tril=self.posterior_scale
        )
        base_samples = dist.sample((n_samples,))
        final_samples = torch.zeros_like(base_samples)
        num_evals = torch.zeros(n_samples)
        times = torch.zeros(n_samples)

        if self.likelihood == "regression":
            for i in range(n_samples):
                # if i % 10 == 0:
                #     self.logger.info(i)
                base_sample = base_samples[i, :]

                t1 = time.time()
                final_sample = geodesic(
                    dim=self.dim,
                    christoffel_fn=self.regression_christoffel_fn,
                    theta=self.mean.squeeze().detach().numpy(),
                    v=base_sample.squeeze().detach().numpy(),
                )
                times[i] = time.time() - t1

                final_samples[i, :] = torch.as_tensor(
                    final_sample["y"][: self.dim, -1], dtype=torch.float32
                )
                num_evals[i] = final_sample["nfev"]

        else:
            self.classification_model = copy.deepcopy(self.model).append(
                torch.nn.Softmax(dim=0)
            )

            for i in range(n_samples):
                # if i % 10 == 0:
                #     self.logger.info(i)
                base_sample = base_samples[i, :]

                t1 = time.time()
                final_sample = geodesic(
                    dim=self.dim,
                    christoffel_fn=self.classification_christoffel_fn,
                    theta=self.mean.squeeze().detach().numpy(),
                    v=base_sample.squeeze().detach().numpy(),
                )
                times[i] = time.time() - t1

                final_samples[i, :] = torch.as_tensor(
                    final_sample["y"][: self.dim, -1], dtype=torch.float32
                )
                num_evals[i] = final_sample["nfev"]

        return final_samples, num_evals, times
