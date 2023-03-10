import numpy as np
import torch


class Layer(torch.nn.Module):
    def __init__(self, input_dim=None, dtype=None, device=None):
        """
        A base class for VIP layers. Basic functionality for multisample
        conditional.

        Parameters
        ----------
        input_dim : int
                    Input dimension
        dtype : data-type
                The dtype of the layer's computations and weights.
        device : torch.device
                 The device in which the computations are made.
        """
        super().__init__()
        self.dtype = dtype
        self.device = device
        self.input_dim = input_dim
        self.freeze = False

    def KL(self):
        raise NotImplementedError

    def forward(self, X):
        raise NotImplementedError


class VIPLayer(Layer):
    def __init__(
        self,
        generative_function,
        num_regression_coeffs,
        input_dim,
        output_dim,
        add_prior_regularization=False,
        log_layer_noise=None,
        q_sqrt_initial_value=1,
        q_mu_initial_value=0,
        mean_function=None,
        dtype=torch.float64,
        device=None,
    ):
        """
        A variational implicit process layer.

        The underlying model performs a Bayesian linear regression
        approximation

        f(x) = mean_function(x) + a^T \phi(x)

        with

        phi(x) = 1/S (f_1(x) - m(x), ..., f_S(x) - m(x)),  a ~ N(0, I)

        Where S randomly sampled functions are used and m(x) denotes
        their empirical mean.

        The variational distribution over the regression coefficients is

            Q(a) = N(q_mu, q_sqrt q_sqrt^T)

        The layer holds D_out independent VIPs.

        Parameters
        ----------
        generative_function : GenerativeFunction
                              Generates function samples using the input
                              locations X and noise values.
        num_regression_coeffs : integer
                                Indicates the amount of linear regression
                                coefficients to use in the approximation.
                                Coincides with the number of samples of f.
        input_dim : int
                    Dimensionality of the given features. Used to
                    pre-fix the shape of the different layers of the model.
        output_dim : int
                      The number of independent VIP in this layer.
                      More precisely, q_mu has shape (S, output_dim)
        add_prior_regularization : bool
                                   Wether to add the prior regularization term
                                   to the layer KL.
        log_layer_noise : float or tf.tensor of shape (output_dim)
                          Contains the noise of each VIP contained in this
                          layer, i.e, epsilon ~ N(0, exp(log_layer_noise))
        q_sqrt_initial_value : float
                               Initial value for the layer initial q_sqrt.
        q_mu_initial_value : float
                             Initial value for the layers initial q_mu.
        mean_function : callable
                        Mean function added to the model. If no mean function
                        is specified, no value is added.
        device : torch.device
                 The device in which the computations are made.
        dtype : data-type
                The dtype of the layer's computations and weights.
        """
        super().__init__(dtype=dtype, input_dim=input_dim, device=device)
        self.add_prior_regularization = add_prior_regularization
        self.num_coeffs = num_regression_coeffs

        # Regression Coefficients prior mean
        self.q_mu = torch.tensor(
            np.ones((self.num_coeffs, output_dim)) * q_mu_initial_value,
            dtype=self.dtype,
            device=self.device,
        )
        self.q_mu = torch.nn.Parameter(self.q_mu)

        # If no mean function is given, constant 0 is used
        self.mean_function = mean_function

        # Verticality of the layer
        self.output_dim = torch.tensor(output_dim, dtype=torch.int32, device=device)

        # Initialize generative function
        self.generative_function = generative_function

        # Initialize the layer's noise
        if log_layer_noise is not None:
            self.log_layer_noise = torch.nn.Parameter(
                torch.tensor(
                    np.ones(output_dim) * log_layer_noise,
                    dtype=self.dtype,
                    device=self.device,
                )
            )
        else:
            self.log_layer_noise = log_layer_noise

        # Define Regression coefficients deviation using tiled triangular
        # identity matrix
        # Shape (num_coeffs, num_coeffs)
        q_sqrt = np.eye(self.num_coeffs) * q_sqrt_initial_value
        # Replicate it output_dim times
        # Shape (num_coeffs, num_coeffs, output_dim)
        q_sqrt = np.tile(q_sqrt[:, :, None], [1, 1, output_dim])
        # Create tensor with triangular representation.
        # Shape (output_dim, num_coeffs*(num_coeffs + 1)/2)
        li, lj = torch.tril_indices(self.num_coeffs, self.num_coeffs)
        triangular_q_sqrt = q_sqrt[li, lj]
        self.q_sqrt_tri = torch.tensor(
            triangular_q_sqrt,
            dtype=self.dtype,
            device=self.device,
        )
        self.q_sqrt_tri = torch.nn.Parameter(self.q_sqrt_tri)

    def forward(self, X, return_prior_samples=False):
        """
        Computes Q*(y|x, a) using the linear regression approximation.
        Given that this distribution is Gaussian and Q(a) is also Gaussian
        the linear regression coefficients, a, can be marginalized, raising a
        Gaussian distribution as follows:

        Let

        phi(x) =   1/sqrt{S}(f_1(x) - m^*(x),...,f_S(\bm x) - m^*(x)),

        with f_1,..., f_S the sampled functions. Then if

        Q^*(y|x,a,\theta) = N(m^*(x) + 1/sqrt{S} phi(x)^T a, sigma^2)

        and

        Q(a) = N(q_mu, q_sqrt q_sqrt^T)

        the marginalized distribution is

        Q^*(y | x, \theta) = N(
            m^*(x) + 1/sqrt{S} phi(x)^T q_mu,
            sigma^2 + phi(x)^T q_sqrt q_sqrt^T phi(x)
        )

        Parameters:
        -----------
        X : torch tensor of shape (N, D)
            Contains the input locations.
        return_prior_samples : boolean
                               Whether to return the generated prior
                               samples or not.

        Returns:
        --------
        mean : torch tensor of shape (N, self.output_dim)
               Mean values of the marginal distribution.
        var : torch tensor of shape (N , self.output_dim)
              Variance values of the marginal distribution.
        prior_samples : torch tensor of shape (self.num_coeffs, N, self.output_dim)
                        Learned prior samples applied to X.
        """

        # Let S = num_coeffs, D = output_dim and N = num_samples
        # Shape (S, N, 1)
        f = self.generative_function(X)
        # Compute mean value, shape (1 , N, 1)
        m = torch.mean(f, dim=0, keepdims=True)

        # Compute regresion function, shape (S , N, 1)
        phi = (f - m) / torch.sqrt(torch.tensor(self.num_coeffs - 1).type(self.dtype))

        # Compute mean value as m + q_mu^T phi per point and output dim
        # q_mu has shape (S, D)
        # phi has shape (S, N, 1)
        mean = m.squeeze(axis=0) + torch.einsum("snd,sd->nd", phi, self.q_mu)

        # Shape (S, S, D)
        q_sqrt = (
            torch.zeros((self.num_coeffs, self.num_coeffs, self.output_dim))
            .to(self.dtype)
            .to(self.device)
        )
        li, lj = torch.tril_indices(self.num_coeffs, self.num_coeffs)
        q_sqrt[li, lj] = self.q_sqrt_tri

        # Compute the diagonal of the predictive covariance matrix
        # K = diag(phi^T q_sqrt^T q_sqrt phi)
        K = torch.einsum("ind, sid -> snd", phi, q_sqrt)
        K = torch.sum(K * K, dim=0)

        # Add layer noise to variance
        if self.log_layer_noise is not None:
            K = K + torch.exp(self.log_layer_noise)

        # Add mean function
        if self.mean_function is not None:
            mean = mean + self.mean_function(X)

        if return_prior_samples:
            return mean, K, f

        return mean, K

    def KL(self):
        """
        Computes the KL divergence from the variational distribution of
        the linear regression coefficients to the prior.

        That is from a Gaussian N(q_mu, q_sqrt) to N(0, I).
        Uses formula for computing KL divergence between two
        multivariate normals, which in this case is:

        KL = 0.5 * ( tr(q_sqrt^T q_sqrt) +
                     q_mu^T q_mu - M - log |q_sqrt^T q_sqrt| )
        """

        # self.q_sqrt_tri stores the triangular matrix using indexes
        #  (0,0), (1,0), (1,1), (2,0), (2,1), (2,2), (3,0)....
        #  knowing this, the diagonal is stored at positions 0, 2, 5, 9, 13...
        #  which can be created using np.cumsum
        diag_indexes = np.cumsum(np.arange(1, self.num_coeffs + 1)) - 1
        diag = self.q_sqrt_tri[diag_indexes]
        # Constant dimensionality term
        KL = -0.5 * self.output_dim * self.num_coeffs

        # Log of determinant of covariance matrix.
        # Det(Sigma) = Det(q_sqrt q_sqrt^T) = Det(q_sqrt) Det(q_sqrt^T)
        #            = prod(diag_s_sqrt)^2
        KL -= torch.sum(torch.log(torch.abs(diag)))

        # Trace term
        KL += 0.5 * torch.sum(torch.square(self.q_sqrt_tri))

        # Mean term
        KL += 0.5 * torch.sum(torch.square(self.q_mu))

        if self.add_prior_regularization:
            KL += self.generative_function.KL()

        return KL

    def freeze_posterior(self):
        """Sets the model parameters as non-trainable."""
        self.q_mu.requires_grad = False
        self.q_sqrt_tri.requires_grad = False
        if self.log_layer_noise:
            self.log_layer_noise.requires_grad = False

    def freeze_prior(self):
        """Sets the prior parameters of this layer as non trainable."""
        self.generative_function.freeze_parameters()




class VIPLayerInducing(Layer):
    def __init__(
        self,
        generative_function,
        Z,
        input_dim,
        output_dim,
        add_prior_regularization=False,
        log_layer_noise=None,
        q_sqrt_initial_value=1,
        q_mu_initial_value=0,
        mean_function=None,
        dtype=torch.float64,
        device=None,
    ):
        super().__init__(dtype=dtype, input_dim=input_dim, device=device)
        self.add_prior_regularization = add_prior_regularization
        self.num_inducing = Z.shape[0]
        self.inducing_points = torch.nn.Parameter(torch.tensor(Z))

        # Regression Coefficients prior mean
        self.q_mu = torch.tensor(
            np.ones((self.num_inducing, output_dim)) * q_mu_initial_value,
            dtype=self.dtype,
            device=self.device,
        )
        self.q_mu = torch.nn.Parameter(self.q_mu)

        # If no mean function is given, constant 0 is used
        self.mean_function = mean_function

        # Verticality of the layer
        self.output_dim = torch.tensor(output_dim, dtype=torch.int32, device=device)

        # Initialize generative function
        self.generative_function = generative_function

        # Initialize the layer's noise
        if log_layer_noise is not None:
            self.log_layer_noise = torch.nn.Parameter(
                torch.tensor(
                    np.ones(output_dim) * log_layer_noise,
                    dtype=self.dtype,
                    device=self.device,
                )
            )
        else:
            self.log_layer_noise = log_layer_noise

        # Define Regression coefficients deviation using tiled triangular
        # identity matrix
        # Shape (num_inducing, num_inducing)
        q_sqrt = np.eye(self.num_inducing) * q_sqrt_initial_value
        # Replicate it output_dim times
        # Shape (num_inducing, num_inducing, output_dim)
        q_sqrt = np.tile(q_sqrt[:, :, None], [1, 1, output_dim])
        # Create tensor with triangular representation.
        # Shape (output_dim, num_inducing*(num_inducing + 1)/2)
        li, lj = torch.tril_indices(self.num_inducing, self.num_inducing)
        triangular_q_sqrt = q_sqrt[li, lj]
        self.q_sqrt_tri = torch.tensor(
            triangular_q_sqrt,
            dtype=self.dtype,
            device=self.device,
        )
        self.q_sqrt_tri = torch.nn.Parameter(self.q_sqrt_tri)

    def forward(self, X, return_prior_samples=False):

        # Let S = num_coeffs, D = output_dim and N = num_samples
        # Shape (S, N, 1)
        X_and_Z = torch.cat([X, self.inducing_points], dim = 0)
        f = self.generative_function(X_and_Z)
        # Compute mean value, shape (1 , N, 1)
        f_mean = torch.mean(f, dim=0, keepdims=True)

        # Compute regresion function, shape (S , N, 1)
        phi = (f - f_mean) / torch.sqrt(torch.tensor(f.shape[0] - 1).type(self.dtype))
        f_K = torch.einsum("and, amd -> nmd", phi, phi)
        
        Ku = f_K[X.shape[0]:, X.shape[0]:].permute(2,0,1) + 2e-6 * torch.eye(self.num_inducing)
        Kf = f_K[:X.shape[0], :X.shape[0]].permute(2,0,1) + 2e-6 * torch.eye(X.shape[0])
        Kfu = f_K[:X.shape[0], X.shape[0]:].permute(2,0,1)
        
        self.Lu = torch.linalg.cholesky(Ku+ 1e-4 * torch.eye(self.num_inducing))

        A = torch.linalg.solve_triangular(self.Lu.permute(0,2,1), Kfu, upper=True, left= False)
        A = torch.linalg.solve_triangular(self.Lu, A, upper=False, left = False)

        mean = torch.einsum("dnm, md -> nd", A, self.q_mu)

        # Shape (S, S, D)
        self.q_sqrt = (
            torch.zeros((self.num_inducing, self.num_inducing, self.output_dim))
            .to(self.dtype)
            .to(self.device)
        )
        li, lj = torch.tril_indices(self.num_inducing, self.num_inducing)
        self.q_sqrt[li, lj] = self.q_sqrt_tri
        
        SK = torch.einsum("nmd, bmd->dnb", self.q_sqrt, self.q_sqrt) - Ku
        
        B = torch.einsum("dmb, dnb -> dmn", SK, A)
        
        delta_cov = torch.sum(A * B.permute(0, 2, 1), -1)
        K = torch.diagonal(Kf, dim1=1, dim2=2) + delta_cov
        
        # Add layer noise to variance
        if self.log_layer_noise is not None:
            K = K + torch.exp(self.log_layer_noise).unsqueeze(-1)

        # Add mean function
        if self.mean_function is not None:
            mean = mean + self.mean_function(X)
            
        if return_prior_samples:
            return mean, K, f[:, :X.shape[0], :]
        return mean, K

    def KL(self):

        # self.q_sqrt_tri stores the triangular matrix using indexes
        #  (0,0), (1,0), (1,1), (2,0), (2,1), (2,2), (3,0)....
        #  knowing this, the diagonal is stored at positions 0, 2, 5, 9, 13...
        #  which can be created using np.cumsum
        diag_indexes = np.cumsum(np.arange(1, self.num_inducing + 1)) - 1
        diag = self.q_sqrt_tri[diag_indexes]
        # Constant dimensionality term
        KL = -0.5 * self.output_dim * self.num_inducing

        # Log of determinant of covariance matrix.
        # Det(Sigma) = Det(q_sqrt q_sqrt^T) = Det(q_sqrt) Det(q_sqrt^T)
        #            = prod(diag_s_sqrt)^2
        KL -= torch.sum(torch.log(torch.abs(diag)))
        
        KL += torch.sum(torch.log(torch.abs(torch.diagonal(self.Lu, dim1=1, dim2=2))))
        \
            
        KL += 0.5 * torch.sum(torch.square(torch.linalg.solve_triangular(self.Lu, self.q_sqrt, upper=False)))
        
        Kinv_m = torch.linalg.solve_triangular(self.Lu, self.q_mu, upper = False)
        KL += 0.5 * torch.sum(self.q_mu * Kinv_m)
        if self.add_prior_regularization:
            KL += self.generative_function.KL()
        return KL

    def freeze_posterior(self):
        """Sets the model parameters as non-trainable."""
        self.q_mu.requires_grad = False
        self.q_sqrt_tri.requires_grad = False
        if self.log_layer_noise:
            self.log_layer_noise.requires_grad = False

    def freeze_prior(self):
        """Sets the prior parameters of this layer as non trainable."""
        self.generative_function.freeze_parameters()




class SparseGP(Layer):
    def __init__(
        self,
        generative_function,
        Z,
        input_dim,
        output_dim,
        add_prior_regularization=False,
        log_layer_noise=None,
        q_sqrt_initial_value=1,
        q_mu_initial_value=0,
        mean_function=None,
        dtype=torch.float64,
        device=None,
    ):
        super().__init__(dtype=dtype, input_dim=input_dim, device=device)
        self.add_prior_regularization = add_prior_regularization
        self.num_inducing = Z.shape[0]
        self.inducing_points = torch.nn.Parameter(torch.tensor(Z))

        # Regression Coefficients prior mean
        self.q_mu = torch.tensor(
            np.ones((self.num_inducing, output_dim)) * q_mu_initial_value,
            dtype=self.dtype,
            device=self.device,
        )
        self.q_mu = torch.nn.Parameter(self.q_mu)

        # If no mean function is given, constant 0 is used
        self.mean_function = mean_function

        # Verticality of the layer
        self.output_dim = torch.tensor(output_dim, dtype=torch.int32, device=device)

        # Initialize the layer's noise
        if log_layer_noise is not None:
            self.log_layer_noise = torch.nn.Parameter(
                torch.tensor(
                    np.ones(output_dim) * log_layer_noise,
                    dtype=self.dtype,
                    device=self.device,
                )
            )
        else:
            self.log_layer_noise = log_layer_noise

        # Define Regression coefficients deviation using tiled triangular
        # identity matrix
        # Shape (num_inducing, num_inducing)
        q_sqrt = np.eye(self.num_inducing) * q_sqrt_initial_value
        # Replicate it output_dim times
        # Shape (num_inducing, num_inducing, output_dim)
        q_sqrt = np.tile(q_sqrt[:, :, None], [1, 1, output_dim])
        # Create tensor with triangular representation.
        # Shape (output_dim, num_inducing*(num_inducing + 1)/2)
        li, lj = torch.tril_indices(self.num_inducing, self.num_inducing)
        triangular_q_sqrt = q_sqrt[li, lj]
        self.q_sqrt_tri = torch.tensor(
            triangular_q_sqrt,
            dtype=self.dtype,
            device=self.device,
        )
        self.q_sqrt_tri = torch.nn.Parameter(self.q_sqrt_tri)
        
        self.log_lengthscale = torch.nn.Parameter(torch.tensor(0., dtype=self.dtype, device = self.device))
        self.log_amplitude = torch.nn.Parameter(torch.tensor(0., dtype=self.dtype, device = self.device))


    def kernel(self, x1, x2 = None):
        # x1 is (N1, D)
        # x2 is (N2, D)
        if x2 is None:
            x2 = x1
            
        x1_ = x1 / torch.exp(self.log_lengthscale)
        x2_ = x2 / torch.exp(self.log_lengthscale)
        
        dist = torch.sum((x1_.unsqueeze(1) - x2_.unsqueeze(0))**2, -1)
        return torch.exp(self.log_amplitude) * torch.exp(-dist).unsqueeze(0)
        
        
    def forward(self, X, return_prior_samples=False):
        
        
        Ku = self.kernel(self.inducing_points) + 2e-6 * torch.eye(self.num_inducing)

        Kf = self.kernel(X) + 2e-6 * torch.eye(X.shape[0])
        Kfu = self.kernel(X, self.inducing_points)
        
        self.Lu = torch.linalg.cholesky(Ku +  1e-4 * torch.eye(self.num_inducing))

        A = torch.linalg.solve_triangular(self.Lu.permute(0,2,1), Kfu, upper=True, left= False)
        A = torch.linalg.solve_triangular(self.Lu, A, upper=False, left = False)

        mean = torch.einsum("dnm, md -> nd", A, self.q_mu)

        # Shape (S, S, D)
        self.q_sqrt = (
            torch.zeros((self.num_inducing, self.num_inducing, self.output_dim))
            .to(self.dtype)
            .to(self.device)
        )
        li, lj = torch.tril_indices(self.num_inducing, self.num_inducing)
        self.q_sqrt[li, lj] = self.q_sqrt_tri
        
        SK = torch.einsum("nmd, bmd->dnb", self.q_sqrt, self.q_sqrt) - Ku
        
        B = torch.einsum("dmb, dnb -> dmn", SK, A)
        
        delta_cov = torch.sum(A * B.permute(0, 2, 1), -1)
        K = torch.diagonal(Kf, dim1=1, dim2=2) + delta_cov
        
        # Add layer noise to variance
        if self.log_layer_noise is not None:
            K = K + torch.exp(self.log_layer_noise)

        # Add mean function
        if self.mean_function is not None:
            mean = mean + self.mean_function(X)
            
        if return_prior_samples:
            return mean, K, torch.zeros(20, X.shape[0], 1)
        return mean, K

    def KL(self):

        # self.q_sqrt_tri stores the triangular matrix using indexes
        #  (0,0), (1,0), (1,1), (2,0), (2,1), (2,2), (3,0)....
        #  knowing this, the diagonal is stored at positions 0, 2, 5, 9, 13...
        #  which can be created using np.cumsum
        diag_indexes = np.cumsum(np.arange(1, self.num_inducing + 1)) - 1
        diag = self.q_sqrt_tri[diag_indexes]
        # Constant dimensionality term
        KL = -0.5 * self.output_dim * self.num_inducing

        # Log of determinant of covariance matrix.
        # Det(Sigma) = Det(q_sqrt q_sqrt^T) = Det(q_sqrt) Det(q_sqrt^T)
        #            = prod(diag_s_sqrt)^2
        KL -= torch.sum(torch.log(torch.abs(diag)))
        
        KL += torch.sum(torch.log(torch.abs(torch.diagonal(self.Lu, dim1=1, dim2=2))))
        \
            
        KL += 0.5 * torch.sum(torch.square(torch.linalg.solve_triangular(self.Lu, self.q_sqrt, upper=False)))
        
        Kinv_m = torch.linalg.solve_triangular(self.Lu, self.q_mu, upper = False)
        KL += 0.5 * torch.sum(self.q_mu * Kinv_m)
        if self.add_prior_regularization:
            KL += self.generative_function.KL()
        return KL

    def freeze_posterior(self):
        """Sets the model parameters as non-trainable."""
        self.q_mu.requires_grad = False
        self.q_sqrt_tri.requires_grad = False
        if self.log_layer_noise:
            self.log_layer_noise.requires_grad = False

    def freeze_prior(self):
        """Sets the prior parameters of this layer as non trainable."""
        self.generative_function.freeze_parameters()
