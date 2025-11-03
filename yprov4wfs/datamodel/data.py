import os
from typing import Any
from yprov4wfs.datamodel.core import Agent, Node

#------------------FileType------------------–#
class FileType:
    def __init__(self, extension: str, mime_type: str):
        self._extension = extension
        self._mime_type = mime_type

    @property
    def extension(self) -> str:
        return self._extension

    @property
    def mime_type(self) -> str:
        return self._mime_type

    # checks if the extension of a given file matches the expected extension for
    # a specific FileType, returning True if they match and False otherwise
    def validate(self, file_path) -> bool:
        _, ext = os.path.splitext(file_path)
        return ext.lower() == self._extension
    

#------------------DATA------------------–#
class Data:
    """
    Represents a data entity with an ID, name and maintains attributes such as
    type, producer, consumer, and associated agent. It also tracks whether the
    data is used as input or outputs.
    """
    def __init__(self, id: str, name: str):
        self._id: str = id
        self._name: str = name
        self._type: FileType | str | None = None
        self._producer: Node | None = None
        self._consumers: list[Node] = []
        self._origins = []
        self._agent: Agent | None = None
        self._is_input: bool = False
        self._is_output: bool = False
        self._info: Any = None
        
    def set_type(self, type: FileType | str):
        self._type = type

    def set_producer(self, producer: Node):
        self._producer = producer
        self._is_output = True

    def add_consumer(self, consumer: Node):
        self._consumers.append(consumer)
        self._is_input = True

    def add_origin(self, origin):
        self._origins.append(origin)

    def is_input(self) -> bool:
        return self._is_input

    def is_output(self) -> bool:
        return self._is_output
    
    def changeType(self, exformat: FileType | str, newformat: FileType | str):
        if self.type == exformat:
            self.type = newformat
        else: 
            raise ValueError("The format of the input is not the expected format")   

    def set_agent(self, agent: Agent):
        self._agent = agent 
        agent._attributed_to.append(self)
