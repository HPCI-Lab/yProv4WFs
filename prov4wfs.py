from abc import ABC, abstractmethod
from datamodel.workflow import Workflow
from datamodel.task import Task
from datamodel.data import Data, FileType
from datamodel.agent import Agent

#------------------PROV4WFS EXECUTOR------------------–#
class Prov4WfsExecutor(ABC):
    @abstractmethod
    async def populate_prov_workflow(self):
        pass
    @abstractmethod
    async def run(self):
        pass
    