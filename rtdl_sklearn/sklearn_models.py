import numpy as np
import torch.optim
from sklearn.base import BaseEstimator, RegressorMixin, ClassifierMixin
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import StandardScaler, OrdinalEncoder
from skorch import NeuralNetRegressor, NeuralNetClassifier
from skorch.callbacks import EarlyStopping
from torch.nn import CrossEntropyLoss

from .dcn2 import DCNv2
from .ft_transformer import FTTransformer
from .mlp import MLP
from .resnet import ResNet


def get_categorical_feature_index(x, threshold=5):
    # automatically determine the categorical feature
    x_num_dim = []
    x_cat_dim = []
    x_cat_cardinalities = []
    for k in range(x.shape[1]):
        count = len(np.unique(x[:, k]))
        if count <= threshold:
            x_cat_dim.append(k)
            x_cat_cardinalities.append(count)
        else:
            x_num_dim.append(k)
    return x_num_dim, x_cat_dim, x_cat_cardinalities


class MLPBase(BaseEstimator):
    def __init__(self, patience=10):
        self.x_transformer = None
        self.y_transformer = None
        self.net = None
        self.patience = patience

    def data_preprocess(self, X, y):
        x = X.astype(np.float32)
        if not isinstance(self, ClassifierMixin):
            y = y.astype(np.float32)
        x_num_dim, x_cat_dim, x_cat_cardinalities = get_categorical_feature_index(X, threshold=5)
        x_transformer = ColumnTransformer(
            [('cat_cols', OrdinalEncoder(handle_unknown='use_encoded_value',
                                         unknown_value=np.nan), x_cat_dim),
             ('num_cols', StandardScaler(), x_num_dim)])
        self.x_transformer = x_transformer
        x = x_transformer.fit_transform(x).astype(np.float32)
        if not isinstance(self, ClassifierMixin):
            y_transformer = StandardScaler()
            self.y_transformer = y_transformer
            y = y_transformer.fit_transform(np.reshape(y, (-1, 1)))
        x_cat_dim = [i for i in range(len(x_cat_dim))]
        x_num_dim = [i for i in range(len(x_cat_dim), len(x_cat_dim) + len(x_num_dim))]
        if len(x_cat_cardinalities) == 0:
            x_cat_cardinalities = None
        return x, y, x_num_dim, x_cat_dim, x_cat_cardinalities

    def predict(self, X):
        x = self.x_transformer.transform(X).astype(np.float32)
        x = np.nan_to_num(x)
        if not isinstance(self, ClassifierMixin):
            y = self.y_transformer.inverse_transform(self.net.predict(x))
        else:
            y = self.net.predict(x)
        return np.nan_to_num(y, posinf=0, neginf=0)


class FTTransformerRegressor(RegressorMixin, MLPBase):
    def __init__(self, n_blocks=3, d_token=192, attention_dropout=0.2, ffn_dropout=0.1, residual_dropout=0,
                 token_bias=False, n_layers=3, n_heads=8, d_ffn_factor=4 / 3, activation='reglu',
                 prenormalization=True, initialization='kaiming', learning_rate=1e-4, weight_decay=1e-5):
        super().__init__()
        self.n_blocks = n_blocks
        self.d_token = d_token
        self.attention_dropout = attention_dropout
        self.ffn_dropout = ffn_dropout
        self.residual_dropout = residual_dropout
        self.token_bias = token_bias
        self.n_layers = n_layers
        self.n_heads = n_heads
        self.d_ffn_factor = d_ffn_factor
        self.activation = activation
        self.prenormalization = prenormalization
        self.initialization = initialization
        self.learning_rate = learning_rate
        self.weight_decay = weight_decay

    def fit(self, X, y):
        x, y, x_num_dim, x_cat_dim, x_cat_cardinalities = self.data_preprocess(X, y)
        model = FTTransformer(
            d_numerical=len(x_num_dim),
            categories=x_cat_cardinalities,
            token_bias=self.token_bias,
            n_layers=self.n_layers,
            d_token=self.d_token,
            n_heads=self.n_heads,
            d_ffn_factor=self.d_ffn_factor,
            attention_dropout=self.attention_dropout,
            ffn_dropout=self.ffn_dropout,
            residual_dropout=self.residual_dropout,
            activation=self.activation,
            prenormalization=self.prenormalization,
            initialization=self.initialization,
            kv_compression=None,
            kv_compression_sharing=None,
            d_out=1,
            feature_index=(x_num_dim, x_cat_dim),
        )
        net = NeuralNetRegressor(
            model,
            max_epochs=200,
            lr=self.learning_rate,
            optimizer=torch.optim.Adam,
            callbacks=[EarlyStopping(patience=self.patience)],
            verbose=False,
            optimizer__weight_decay=self.weight_decay,
        )
        self.net = net
        net.fit(x, y)


class MLPRegressor(MLPBase):
    def __init__(self,
                 layers=2,
                 layer_size=32,
                 dropout=0,
                 categorical_embedding_size=8,
                 learning_rate=1e-4, weight_decay=1e-5):
        super().__init__()
        self.layers = layers
        self.layer_size = layer_size
        self.d_layers = [int(layer_size), ] * int(layers)
        self.dropout = dropout
        self.categorical_embedding_size = int(categorical_embedding_size)
        self.learning_rate = learning_rate
        self.weight_decay = weight_decay

    def fit(self, X, y):
        x, y, x_num_dim, x_cat_dim, x_cat_cardinalities = self.data_preprocess(X, y)
        model = MLP(
            d_in=len(x_num_dim),
            d_layers=self.d_layers,
            d_out=1,
            dropout=self.dropout,
            categories=x_cat_cardinalities,
            d_embedding=self.categorical_embedding_size,
            feature_index=(x_num_dim, x_cat_dim),
        )
        net = NeuralNetRegressor(
            model,
            max_epochs=200,
            lr=self.learning_rate,
            optimizer=torch.optim.Adam,
            callbacks=[EarlyStopping(patience=self.patience)],
            verbose=False,
            optimizer__weight_decay=self.weight_decay,
        )
        self.net = net
        net.fit(x, y)


class DCNV2Regressor(MLPBase):
    def __init__(self,
                 cross_layers=2,
                 hidden_layers=2,
                 layer_size=32,
                 hidden_dropout=0,
                 cross_dropout=0,
                 categorical_embedding_size=8,
                 learning_rate=1e-4, weight_decay=1e-5):
        super().__init__()
        self.layer_size = int(layer_size)
        self.cross_layers = int(cross_layers)
        self.hidden_layers = int(hidden_layers)
        self.hidden_dropout = hidden_dropout
        self.cross_dropout = cross_dropout
        self.categorical_embedding_size = int(categorical_embedding_size)
        self.learning_rate = learning_rate
        self.weight_decay = weight_decay

    def fit(self, X, y):
        x, y, x_num_dim, x_cat_dim, x_cat_cardinalities = self.data_preprocess(X, y)
        model = DCNv2(
            d_in=len(x_num_dim),
            d=self.layer_size,
            n_hidden_layers=self.hidden_layers,
            n_cross_layers=self.cross_layers,
            hidden_dropout=self.hidden_dropout,
            cross_dropout=self.cross_dropout,
            d_out=1,
            stacked=True,
            categories=x_cat_cardinalities,
            d_embedding=self.categorical_embedding_size,
            feature_index=(x_num_dim, x_cat_dim),
        )
        net = NeuralNetRegressor(
            model,
            max_epochs=200,
            lr=self.learning_rate,
            optimizer=torch.optim.Adam,
            callbacks=[EarlyStopping(patience=self.patience)],
            verbose=False,
            optimizer__weight_decay=self.weight_decay,
        )
        self.net = net
        net.fit(x, y)


class ResNetRegressor(MLPBase):
    def __init__(self,
                 hidden_layers=2,
                 layer_size=32,
                 d_hidden_factor=1,
                 hidden_dropout=0,
                 residual_dropout=0,
                 categorical_embedding_size=8,
                 learning_rate=1e-4, weight_decay=1e-5):
        super().__init__()
        self.layer_size = int(layer_size)
        self.d_hidden_factor = d_hidden_factor
        self.hidden_layers = int(hidden_layers)
        self.hidden_dropout = hidden_dropout
        self.residual_dropout = residual_dropout
        self.categorical_embedding_size = int(categorical_embedding_size)
        self.learning_rate = learning_rate
        self.weight_decay = weight_decay

    def fit(self, X, y):
        x, y, x_num_dim, x_cat_dim, x_cat_cardinalities = self.data_preprocess(X, y)
        model = ResNet(
            d_numerical=len(x_num_dim),
            categories=x_cat_cardinalities,
            d_embedding=self.categorical_embedding_size,
            d=self.layer_size,
            d_hidden_factor=self.d_hidden_factor,
            n_layers=self.hidden_layers,
            activation='reglu',
            normalization='batchnorm',
            hidden_dropout=self.hidden_dropout,
            residual_dropout=self.residual_dropout,
            d_out=1,
            feature_index=(x_num_dim, x_cat_dim),
        )
        net = NeuralNetRegressor(
            model,
            max_epochs=200,
            lr=self.learning_rate,
            optimizer=torch.optim.Adam,
            callbacks=[EarlyStopping(patience=self.patience)],
            verbose=False,
            optimizer__weight_decay=self.weight_decay,
        )
        self.net = net
        net.fit(x, y)


class FTTransformerClassifier(ClassifierMixin, FTTransformerRegressor):

    def fit(self, X, y):
        x, y, x_num_dim, x_cat_dim, x_cat_cardinalities = self.data_preprocess(X, y)
        model = FTTransformer(
            d_numerical=len(x_num_dim),
            categories=x_cat_cardinalities,
            token_bias=self.token_bias,
            n_layers=self.n_layers,
            d_token=self.d_token,
            n_heads=self.n_heads,
            d_ffn_factor=self.d_ffn_factor,
            attention_dropout=self.attention_dropout,
            ffn_dropout=self.ffn_dropout,
            residual_dropout=self.residual_dropout,
            activation=self.activation,
            prenormalization=self.prenormalization,
            initialization=self.initialization,
            kv_compression=None,
            kv_compression_sharing=None,
            d_out=len(np.unique(y)),
            feature_index=(x_num_dim, x_cat_dim),
        )
        net = NeuralNetClassifier(
            model,
            criterion=CrossEntropyLoss,
            max_epochs=200,
            lr=self.learning_rate,
            optimizer=torch.optim.Adam,
            callbacks=[EarlyStopping(patience=self.patience)],
            verbose=False,
            optimizer__weight_decay=self.weight_decay,
        )
        self.net = net
        net.fit(x, y)


class ResNetClassifier(ClassifierMixin, ResNetRegressor):

    def fit(self, X, y):
        x, y, x_num_dim, x_cat_dim, x_cat_cardinalities = self.data_preprocess(X, y)
        model = ResNet(
            d_numerical=len(x_num_dim),
            categories=x_cat_cardinalities,
            d_embedding=self.categorical_embedding_size,
            d=self.layer_size,
            d_hidden_factor=self.d_hidden_factor,
            n_layers=self.hidden_layers,
            activation='reglu',
            normalization='batchnorm',
            hidden_dropout=self.hidden_dropout,
            residual_dropout=self.residual_dropout,
            d_out=len(np.unique(y)),
            feature_index=(x_num_dim, x_cat_dim),
        )
        net = NeuralNetClassifier(
            model,
            criterion=CrossEntropyLoss,
            max_epochs=200,
            lr=self.learning_rate,
            optimizer=torch.optim.Adam,
            callbacks=[EarlyStopping(patience=self.patience)],
            verbose=False,
            optimizer__weight_decay=self.weight_decay,
        )
        self.net = net
        net.fit(x, y)


class DCNV2Classifier(ClassifierMixin, DCNV2Regressor):

    def fit(self, X, y):
        x, y, x_num_dim, x_cat_dim, x_cat_cardinalities = self.data_preprocess(X, y)
        model = DCNv2(
            d_in=len(x_num_dim),
            d=self.layer_size,
            n_hidden_layers=self.hidden_layers,
            n_cross_layers=self.cross_layers,
            hidden_dropout=self.hidden_dropout,
            cross_dropout=self.cross_dropout,
            d_out=len(np.unique(y)),
            stacked=True,
            categories=x_cat_cardinalities,
            d_embedding=self.categorical_embedding_size,
            feature_index=(x_num_dim, x_cat_dim),
        )
        net = NeuralNetClassifier(
            model,
            criterion=CrossEntropyLoss,
            max_epochs=200,
            lr=self.learning_rate,
            optimizer=torch.optim.Adam,
            callbacks=[EarlyStopping(patience=self.patience)],
            verbose=False,
            optimizer__weight_decay=self.weight_decay,
        )
        self.net = net
        net.fit(x, y)


class MLPClassifier(ClassifierMixin, MLPRegressor):

    def fit(self, X, y):
        x, y, x_num_dim, x_cat_dim, x_cat_cardinalities = self.data_preprocess(X, y)
        model = MLP(
            d_in=len(x_num_dim),
            d_layers=self.d_layers,
            d_out=len(np.unique(y)),
            dropout=self.dropout,
            categories=x_cat_cardinalities,
            d_embedding=self.categorical_embedding_size,
            feature_index=(x_num_dim, x_cat_dim),
        )
        net = NeuralNetClassifier(
            model,
            criterion=CrossEntropyLoss,
            max_epochs=200,
            lr=self.learning_rate,
            optimizer=torch.optim.Adam,
            callbacks=[EarlyStopping(patience=self.patience)],
            verbose=False,
            optimizer__weight_decay=self.weight_decay,
        )
        self.net = net
        net.fit(x, y)
