import warnings
from collections import OrderedDict
from pathlib import Path

import flwr as fl
import torch
import typer
from lightning.fabric import Fabric
from omegaconf import OmegaConf
from typing_extensions import Annotated

from src.console import console
from src.flower.loops_fabric import test_loop, train_loop
from src.ml.registry import data_registry, model_registry

torch.backends.cudnn.enabled = True


class FlowerClient(fl.client.NumPyClient):
    def __init__(
        self,
        cid,
        model,
        train_dataloader,
        validation_dataloader,
        num_examples,
        conf_fabric,
    ) -> None:
        super().__init__()
        self.cid = cid
        self.model = model
        self.train_dataloader = train_dataloader
        self.validation_dataloader = validation_dataloader
        self.conf_fabric = conf_fabric
        self.num_examples = num_examples

        self.optimizer = self.model.configure_optimizers()

        self.fabric = Fabric(**self.conf_fabric)

    def get_parameters(self, config):
        console.log(f"[Client] get_parameters, config: {config}")
        return [val.cpu().numpy() for _, val in self.model.state_dict().items()]

    def set_parameters(self, parameters):
        console.log("[Client] set_parameters")
        params_dict = zip(self.model.state_dict().keys(), parameters)
        state_dict = OrderedDict({k: torch.tensor(v) for k, v in params_dict})
        self.model.load_state_dict(state_dict, strict=True)

    def fit(self, parameters, config):
        console.log(f"[Client {self.cid}] fit, config: {config}")
        self.set_parameters(parameters)
        console.log(f"Round {config['server_round']}, training Started...")
        self.fabric.launch()
        _model, _optimizer = self.fabric.setup(self.model, self.optimizer)
        _train_dataloader = self.fabric.setup_dataloaders(self.train_dataloader)
        loss, accuracy = train_loop(
            self.fabric,
            _model,
            _train_dataloader,
            _optimizer,
            epochs=config["local_epochs"],
        )
        console.log(f"Training Finished! Loss is {loss}")
        metrics = {"accuracy": accuracy, "loss": loss, "cid": self.cid}
        return self.get_parameters(config={}), self.num_examples["trainset"], metrics

    def evaluate(self, parameters, config):
        console.log(f"[Client {self.cid}] evaluate, config: {config}")
        self.set_parameters(parameters)
        console.log(f"Round {config['server_round']}, evaluation Started...")
        self.fabric.launch()
        _model = self.fabric.setup_module(self.model)
        _validation_dataloader = self.fabric.setup_dataloaders(
            self.validation_dataloader
        )
        loss, accuracy = test_loop(self.fabric, _model, _validation_dataloader)
        console.log(f"Evaluation finished! Loss is {loss}, accuracy is {accuracy}")
        metrics = {"accuracy": accuracy, "loss": loss, "cid": self.cid}
        return loss, self.num_examples["valset"], metrics


app = typer.Typer(pretty_exceptions_show_locals=False)


@app.command()
def launch_config(
    config: Annotated[Path, typer.Argument()],
    cid: int = None,
    device_num: int = None,
    root_dir: str = None,
    server_adress: str = None,
):
    """Launch a FlowerClient

    **Args:**

        * device_num (int): the GPU index on which to run the client

        * root_dir (str): absolute path to all datasets

        * data_dir_train (str, optional): relative path to training data.
        Defaults to "/home/T0254685/code_unet/datasets/preprocessed_client1/train/".

        * data_dir_val (str, optional): relative path to validation data.
        Defaults to "/home/T0254685/code_unet/datasets/preprocessed_client1/val/".
    """

    if config is None:
        print("No config file")
        raise typer.Abort()
    if config.is_file():
        conf = OmegaConf.load(config)
    elif config.is_dir():
        print("Config is a directory, will use all its config files")
        raise typer.Abort()
    elif not config.exists():
        print("The config doesn't exist")
        raise typer.Abort()

    if cid is not None:
        conf["cid"] = cid
    if device_num is not None:
        conf["device_num"] = device_num
    if root_dir is not None:
        conf["root_dir"] = root_dir
    if server_adress is not None:
        conf["server_adress"] = server_adress

    trainloader, testloader, num_examples = data_registry[conf["data"]["name"]](
        **conf["data"]["config"]
    )
    console.log(f"Conf specified: {conf}")
    warnings.filterwarnings("ignore", category=UserWarning)

    net = model_registry[conf["model"]["name"]](**conf["model"]["config"])
    client = FlowerClient(
        cid=conf["cid"],
        model=net,
        train_dataloader=trainloader,
        validation_dataloader=testloader,
        num_examples=num_examples,
        conf_fabric=conf["fabric"],
    )
    fl.client.start_numpy_client(
        server_address=conf["server_adress"],
        client=client,
    )


if __name__ == "__main__":
    app()