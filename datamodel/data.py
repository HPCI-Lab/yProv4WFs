import os
from typing import Union
from datamodel.node import Node
from datamodel.task import Task
from datamodel.workflow import Workflow
from datamodel.enactor import Enactor

#------------------FileType------------------–#
class FileType:
    def __init__(self, extension: str, mime_type: str):
        self._extension = extension
        self._mime_type = mime_type

    @property
    def extension(self):
        return self._extension

    @property
    def mime_type(self):
        return self._mime_type

    # checks if the extension of a given file matches the expected extension for a specific FileType,
    # returning True if they match and False otherwise
    def validate(self, file_path):
        _, ext = os.path.splitext(file_path)
        return ext.lower() == self._extension
    

#------------------DATA------------------–#
class Data:
    def __init__(self, id: str, name: str, type: FileType, producer: Union['Task', 'Workflow'], consumer: Union['Task', 'Workflow']):
        self._id = id
        self._name = name
        self._type = type
        self._producer = producer
        self._consumer = consumer
        self._enactor = None

    def is_output(self, producer: Union['Task', 'Workflow']):
        if not isinstance(producer, (Task, Workflow)):
            raise ValueError("producer must be an instance of Task or Workflow")
        return self._producer == producer

    def is_input(self, consumer: Union['Task', 'Workflow']):
        if not isinstance(consumer, (Task, Workflow)):
            raise ValueError("consumer must be an instance of Task or Workflow")
        return self._consumer == consumer
    
    def changeType(self, exformat: FileType, newformat: FileType):
        if self.type == exformat:
            self.type = newformat
        else: 
            raise ValueError("The format of the input is not the expected format")   

    def set_enactor(self, enactor: 'Enactor'):
        self._enactor = enactor 


