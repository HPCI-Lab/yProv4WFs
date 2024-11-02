import os
from yprov4wfs.datamodel.agent import Agent

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
    def __init__(self, id: str, name: str):
        self._id = id
        self._name = name
        self._type = None
        self._producer = None
        self._consumer = None
        self._agent = None
        self.is_input = False
        self.is_output = False
        
    def set_type(self, type: FileType):
        self._type = type

    def set_producer(self, producer: str):
        self._producer = producer
        self.is_output = True

    def set_consumer(self, consumer: str):
        self._consumer = consumer
        self.is_input = True

    def is_input(self):
        return self.is_input

    def is_output(self):
        return self.is_output
    
    def changeType(self, exformat: FileType, newformat: FileType):
        if self.type == exformat:
            self.type = newformat
        else: 
            raise ValueError("The format of the input is not the expected format")   

    def set_agent(self, agent: 'Agent'):
        self._agent = agent 
        agent._attributed_to.append(self)