import logging

import pytorch_lightning as pl
import torch
import torch.nn.functional as F
from torch import nn

from predict_pv_yield.models.base_model import BaseModel

logging.basicConfig()
_LOG = logging.getLogger("predict_pv_yield")


class Model(BaseModel):

    name = 'conv3d'

    def __init__(
        self,
        include_pv_yield: bool = True,
        include_nwp: bool = True,
        include_time: bool = True,
        forecast_len: int = 6,
        history_len: int = 12,
        number_of_conv3d_layers: int = 4,
        conv3d_channels: int = 32,
        image_size_pixels: int = 64,
        number_sat_channels: int = 12,
        fc1_output_features: int = 128,
        fc2_output_features: int = 128,
        fc3_output_features: int = 64,
    ):
        """
        3d conv model, that takes in different data streams

        architecture is roughly satellite image time series goes into many 3d convolution layers.
        Final convolutional layer goes to full connected layer. This is joined by other data inputs like
        - pv yield
        - nwp data
        - time variables
        Then there ~4 fully connected layers which end up forecasting the pv yield intp the future

        include_pv_yield: include pv yield data
        include_nwp: include nwp data
        include_time: include hour of data, and day of year as sin and cos components
        forecast_len: the amount of timesteps that should be forecasted
        history_len: the amount of historical timesteps that are used
        number_of_conv3d_layers, number of convolution 3d layers that are use
        conv3d_channels, the amount of convolution 3d channels
        image_size_pixels: the input satellite image size
        number_sat_channels: number of nwp channels
        fc1_output_features: number of fully connected outputs nodes out of the the first fully connected layer
        fc2_output_features: number of fully connected outputs nodes out of the the second fully connected layer
        fc3_output_features: number of fully connected outputs nodes out of the the third fully connected layer
        """

        self.forecast_len = forecast_len
        self.history_len = history_len
        self.include_pv_yield = include_pv_yield
        self.include_nwp = include_nwp
        self.include_time = include_time
        self.number_of_conv3d_layers = number_of_conv3d_layers
        self.number_of_nwp_features = 10*19*2*2
        self.fc1_output_features = fc1_output_features
        self.fc2_output_features = fc2_output_features
        self.fc3_output_features = fc3_output_features

        super().__init__()

        conv3d_channels = conv3d_channels

        self.number_of_nwp_features = 10 * 19 * 2 * 2

        self.cnn_output_size = (
            conv3d_channels
            * ((image_size_pixels - 2 * self.number_of_conv3d_layers) ** 2)
            * (self.forecast_len + self.history_len + 1 - 2 * self.number_of_conv3d_layers)
        )

        self.sat_conv0 = nn.Conv3d(
            in_channels=number_sat_channels,
            out_channels=conv3d_channels,
            kernel_size=(3, 3, 3),
            padding=0,
        )
        for i in range(0, self.number_of_conv3d_layers - 1):
            layer = nn.Conv3d(
                in_channels=conv3d_channels, out_channels=conv3d_channels, kernel_size=(3, 3, 3), padding=0
            )
            setattr(self, f'conv3d_{i + 1}', layer)

        self.fc1 = nn.Linear(in_features=self.cnn_output_size, out_features=self.fc1_output_features)
        self.fc2 = nn.Linear(in_features=self.fc1_output_features, out_features=self.fc2_output_features)

        fc3_in_features = self.fc2_output_features
        if include_pv_yield:
            fc3_in_features += 128 * 7  # 7 could be (history_len + 1)
        if include_nwp:
            self.fc_nwp = nn.Linear(in_features=self.number_of_nwp_features, out_features=128)
            fc3_in_features += 128
        if include_time:
            fc3_in_features += 4

        self.fc3 = nn.Linear(in_features=fc3_in_features, out_features=self.fc3_output_features)
        self.fc4 = nn.Linear(in_features=self.fc3_output_features, out_features=self.forecast_len)
        # self.fc5 = nn.Linear(in_features=32, out_features=8)
        # self.fc6 = nn.Linear(in_features=8, out_features=1)

    def forward(self, x):
        # ******************* Satellite imagery *************************
        # Shape: batch_size, seq_length, width, height, channel
        sat_data = x["sat_data"]
        batch_size, seq_len, width, height, n_chans = sat_data.shape

        # Conv3d expects channels to be the 2nd dim, https://pytorch.org/docs/stable/generated/torch.nn.Conv3d.html
        sat_data = sat_data.permute(0, 4, 1, 3, 2)
        # Now shape: batch_size, n_chans, seq_len, height, width

        # :) Pass data through the network :)
        out = F.relu(self.sat_conv0(sat_data))
        for i in range(0, self.number_of_conv3d_layers - 1):
            layer = getattr(self, f'conv3d_{i + 1}')
            out = F.relu(layer(out))

        out = out.reshape(batch_size, self.cnn_output_size)

        # Fully connected layers
        out = F.relu(self.fc1(out))
        out = F.relu(self.fc2(out))
        # which has shape (batch_size, 128)

        # add pv yield
        if self.include_pv_yield:
            pv_yield_history = x["pv_yield"][:, : self.history_len + 1].nan_to_num(nan=0.0)

            pv_yield_history = pv_yield_history.reshape(
                pv_yield_history.shape[0], pv_yield_history.shape[1] * pv_yield_history.shape[2]
            )
            out = torch.cat((out, pv_yield_history), dim=1)

        # *********************** NWP Data ************************************
        if self.include_nwp:
            # Shape: batch_size, channel, seq_length, width, height
            nwp_data = x['nwp']
            nwp_data = nwp_data.flatten(start_dim=1)

            # fully connected layer
            out_nwp = F.relu(self.fc_nwp(nwp_data))

            # join with other FC layer
            out = torch.cat((out, out_nwp), dim=1)

        # ########## include time variables #########
        if self.include_time:
            # just take the value now
            x_sin_hour = x["hour_of_day_sin"][:, self.history_len + 1].unsqueeze(dim=1)
            x_cos_hour = x["hour_of_day_cos"][:, self.history_len + 1].unsqueeze(dim=1)
            x_sin_day = x["day_of_year_sin"][:, self.history_len + 1].unsqueeze(dim=1)
            x_cos_day = x["day_of_year_cos"][:, self.history_len + 1].unsqueeze(dim=1)

            out = torch.cat((out, x_sin_hour, x_cos_hour, x_sin_day, x_cos_day), dim=1)

        # Fully connected layers.
        out = F.relu(self.fc3(out))
        out = self.fc4(out)

        out = out.reshape(batch_size, self.forecast_len)

        return out
