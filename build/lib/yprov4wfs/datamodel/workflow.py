from yprov4wfs.datamodel.node import Node
from yprov4wfs.datamodel.data import Data
from yprov4wfs.datamodel.task import Task

from pathlib import Path
from uuid import uuid4
import traceback
import json
import os
import logging

# Set up logging with the requested format
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - yprov4wfs - %(levelname)s - %(message)s'
)

#------------------WORKFLOW------------------–# 
"""
Workflow class represents a workflow in the system, inheriting from Node.
Methods:
    __init__(id: str, name: str):
        Initializes a new instance of the Workflow class.
    add_input(data: Data):
        Adds an input Data object to the workflow.
    add_output(data: Data):
        Adds an output Data object to the workflow.
    add_task(task: Task):
        Adds a Task object to the workflow.
    get_task_by_id(id: str):
        Retrieves a Task object by its ID.
    to_prov():
        Converts the workflow to a PROV document in JSON format without dependencies on the prov.model library.
    prov_to_json(directory_path: str or None):
        Serializes the workflow to a JSON file in the specified directory or the current directory if no path is provided.
"""

class Workflow(Node):
    def __init__(self, id: str, name: str):
        super().__init__(id, name)
        self._inputs: list[Data] = []
        self._outputs: list[Data] = []
        self._tasks: list[Task] = [] # Maybe a dict[str, Task] would be better
        self._num_tasks: int = 0
        self._tasks_done: list[Task] = []
        self._tasks_failed: list[Task] = []
        self._taks_skipped: list[Task] = []
        self._type: str | None = None
        self._engineWMS: str | None = None
        self._resource_cwl_uri = None
        ############# for tracking the data ###########
        self._data: list[Data] = [] # Maybe a dict[str, Data] would be better
        logging.debug("Initialized Workflow with id=%s, name=%s", id, name)
        
    def add_input(self, data: Data):
        data.add_consumer(self)
        if data.is_input:
            self._inputs.append(data)
        #     logging.info("Added input data: %s", data)
        # else:
        #     logging.warning("Tried to add non-input data as input: %s", data)
            
    def add_output(self, data: Data):
        data.set_producer(self)
        if data.is_output:
            self._outputs.append(data)
            # logging.info("Added output data: %s", data)
        # else:
            # logging.warning("Tried to add non-output data as output: %s", data)
            
    def add_task(self, task: Task): 
        self._tasks.append(task)
        logging.info("Added task: %s", task)
        
    def get_task_by_id(self, id: str) -> Task | None:
        for task in self._tasks:
            if task._id == id:
                # logging.debug("Found task by id: %s", id)
                return task
        # logging.warning("Task not found by id: %s", id)
        return None

    ################## adding and retriving data from workflow ###########
    def add_data(self, item: Data):
        if not isinstance(item, Data):
            # logging.error("Tried to add non-Data item: %r", item)
            raise TypeError(f"Expected an instance of [yprov4wfs.datamodel.data] Data, got {type(item).__name__}")
        self._data.append(item)
        # logging.info("Added data item: %s", item)

    def get_data_by_id(self, id: str) -> Data | None:
        for data in self._data:
            if data._id == id:
                # logging.debug("Found data by id: %s", id)
                return data
        # logging.warning("Data not found by id: %s", id)

    # to_prov function without dependences to prov.model library
    def to_prov(self) -> str | None:
        try:
            logging.debug("Starting to_prov serialization")
            doc = {
                'prefix': {
                    'default': 'http://anotherexample.org/',
                    'yprov4wfs': 'http://example.org'
                },
                'activity': {},
                'entity': {},
                'agent': {},
                'used': {},
                'wasGeneratedBy': {},
                'wasAssociatedWith': {},
                'wasAttributedTo': {},
                'actedOnBehalfOf': {},
                'wasInformedBy': {}
            }

            doc['activity'][self._id] = {
                'prov:startTime': self._start_time,
                'prov:endTime': self._end_time,
                'prov:label': self._name,
                'prov:type': 'prov:Activity',
                'yprov4wfs:level': self._level,
                'yprov4wfs:engine': self._engineWMS,
                'yprov4wfs:status': self._status,
            }
            # logging.debug("Added main activity to doc: %s", self._id)
            if self._resource_cwl_uri is not None:
                doc['activity'][self._id]['yprov4wfs:resource_uri'] = self._resource_cwl_uri
                # logging.debug("Added resource_cwl_uri: %s", self._resource_cwl_uri)
            if self._type is not None:
                doc['activity'][self._id]['yprov4wfs:type'] = self._type
                # logging.debug("Added type: %s", self._type)
            if self._description is not None:
                doc['activity'][self._id]['yprov4wfs:description'] = self._description
                # logging.debug("Added description: %s", self._description)


            for input in self._inputs:
                if input is not None:
                    doc['entity'][input._id] = {
                        'prov:label': input._name,
                        'prov:type': 'prov:Entity'
                    }
                    doc['used'][f'{str(uuid4())}'] = {'prov:activity': self._id, 'prov:entity': input._id}
                    # logging.debug("Processed input: %s", input._id)

            for output in self._outputs:
                if output is not None:
                    doc['entity'][output._id] = {
                        'prov:label': output._name,
                        'prov:type': 'prov:Entity'
                    }
                    doc['wasGeneratedBy'][f'{str(uuid4())}'] = {'prov:entity': output._id, 'prov:activity': self._id}
                    # logging.debug("Processed output: %s", output._id)
            
            for task in self._tasks:
                if task is not None:
                    #doc['activity'][task._id] = {
                    task_items = {
                        'prov:startTime': task._start_time,
                        'prov:endTime': task._end_time,
                        'prov:label': task._name,
                        'prov:type': 'prov:Activity',
                        'yprov4wfs:status': task._status,
                        'yprov4wfs:level': task._level
                    }
                    ### parse the _info dict to auto extract the metrics
                    if isinstance(task._info, dict):
                        for key, value in task._info.items():
                            if value is not None:
                                task_items[f'yprov4wfs:{key}'] = str(Workflow.convert_value(value))[-60:]
                    doc['activity'][task._id] = task_items
                    # logging.debug("Processed task: %s", task._id)
                    if task._manual_submit is not None:
                        doc['activity'][task._id]['yprov4wfs:manual_submit'] = task._manual_submit
                    if task._run_platform is not None:
                        doc['activity'][task._id]['yprov4wfs:run_platform'] = task._run_platform
                    if task._delay is not None:
                        doc['activity'][task._id]['yprov4wfs:delay'] = task._delay
                    if task._timeout is not None:
                        doc['activity'][task._id]['yprov4wfs:timeout'] = task._timeout

                    if task._agent is not None:
                        doc['agent'][task._agent._id] = {
                            'prov:label': task._agent._name,
                            'prov:type': 'prov:Agent'
                        }
                        for data_item in task._agent._attributed_to:
                            if data_item is not None:
                                doc['entity'][data_item._id] = {
                                    'prov:label': data_item._name,
                                    'prov:type': 'prov:Entity'
                                }
                                doc['wasAttributedTo'][f'{str(uuid4())}'] = {'prov:entity': data_item._id, 'prov:agent': task._agent._id}

                        if task._agent._acted_for is not None:
                            doc['agent'][task._agent._acted_for._id] = {
                                'prov:label': task._agent._acted_for._name,
                                'prov:type': 'prov:Agent'
                            }
                            doc['actedOnBehalfOf'][f'{str(uuid4())}'] = {'prov:delegate': task._agent._id, 'prov:responsible': task._agent._acted_for._id}

                        doc['wasAssociatedWith'][f'{str(uuid4())}'] = {'prov:activity': task._id, 'prov:agent': task._agent._id}

                    for data_item in task._inputs:
                        if data_item is not None:
                            #doc['entity'][data_item._id] = {
                            entity_entry = {
                                'prov:label': data_item._name,
                                'prov:type': 'prov:Entity',
                            }
                            # parse the _info dict to auto extract the metrics for a data item (inputs)
                            if isinstance(data_item._info, dict):
                                for key, value in data_item._info.items():
                                    if value is not None:
                                        entity_entry[f'yprov4wfs:{key}'] = str(Workflow.convert_value(value))[-60:]

                            doc['entity'][data_item._id] = entity_entry
                            doc['used'][f'{str(uuid4())}'] = {'prov:activity': task._id, 'prov:entity': data_item._id}
                            # logging.debug("Processed task input: %s", data_item._id)
                    for data_item in task._outputs:
                        if data_item is not None:
                            #doc['entity'][data_item._id] = {
                            entity_entry = {
                                'prov:label': data_item._name,
                                'prov:type': 'prov:Entity',
                            }
                            if isinstance(data_item._info, dict):
                                for key, value in data_item._info.items():
                                    if value is not None:
                                        entity_entry[f'yprov4wfs:{key}'] = str(Workflow.convert_value(value))[-60:]

                            doc['entity'][data_item._id] = entity_entry
                            doc['wasGeneratedBy'][f'{str(uuid4())}'] = {'prov:entity': data_item._id, 'prov:activity': task._id}

                    # if task._prev is not None:
                    #     for prev_task in task._prev:
                    #         doc['wasInformedBy'][f'{str(uuid4())}'] = {'prov:informed': task._id, 'prov:informant': prev_task._id}
                            
                    if task._next is not None and not isinstance(task._next, type):
                        valid_next_tasks = [t for t in task._next if t is not None and not isinstance(t, type)]
                        for next_task in valid_next_tasks:
                            doc['wasInformedBy'][f'{str(uuid4())}'] = {'prov:informed': task._id, 'prov:informant': next_task._id}
                            # logging.debug("Processed task next: %s", next_task._id)
                            
            # Helper function to remove empty lists from the dictionary
            def preprocess(obj):
                """
                Recursively preprocess a dictionary or list to:
                1. Remove empty lists from dictionaries or lists.
                2. Replace None/null values with the string "None".
                3. Clean extraneous spaces in strings (but keep empty strings as is).
                """
                if isinstance(obj, dict):
                    return {
                        k.strip() if k is not None else k: preprocess(v) 
                        for k, v in obj.items() 
                        if v != []  # Remove keys with empty list values
                    }
                elif isinstance(obj, list):
                    return [
                        preprocess(i) 
                        for i in obj 
                        if i != []  # Remove empty lists from the list
                    ]
                elif obj is None:
                    return "None"
                elif isinstance(obj, str):
                    cleaned = obj.strip()  # Clean spaces from strings
                    return cleaned
                else:
                    return obj

            doc = preprocess(doc)
            logging.debug("Preprocessed doc for serialization")

            def convert(obj):
                if isinstance(obj, Path):
                    return str(obj)
                elif obj is None:
                    return "None"
                else:
                    logging.error(
                        "JSON serialization error: object=%r, type=%s, location=Workflow.to_prov.convert",
                        obj, type(obj)
                    )
                    raise TypeError(f"Object {obj} of type {type(obj)} is not JSON serializable")
            
            result = json.dumps(doc, indent=4, ensure_ascii=False, default=convert)
            # result = json.dumps(doc, indent=4, default=convert)
            logging.info("Successfully serialized workflow to JSON")
            return result
        except Exception as e:
            logging.error(
                "Error in to_prov: %s\nType: %s\nTraceback:\n%s",
                str(e),
                type(e).__name__,
                traceback.format_exc()
            )
            print(f"Error: {e} ")
            traceback.print_exc()
            return None

  
    def prov_to_json(self, directory_path=None):
        try:
            logging.debug("Starting prov_to_json with directory_path=%s", directory_path)
            if directory_path is None:
                prov_json = self.to_prov()
                if prov_json is None:
                    logging.error("Failed to serialize the document to JSON (to_prov returned None)")
                    raise ValueError("Failed to serialize the document to JSON.")
                json_file_path = f'yprov4wfs.json'
            else:
                os.makedirs(directory_path, exist_ok=True)
                logging.debug("Created directory: %s", directory_path)
                prov_json = self.to_prov()
                if prov_json is None:
                    logging.error("Failed to serialize the document to JSON (to_prov returned None)")
                    raise ValueError("Failed to serialize the document to JSON.")
                json_file_path = os.path.join(directory_path, f'yprov4wfs.json')

            with open(json_file_path, 'w') as f:
                f.write(prov_json)
                logging.info("Wrote prov JSON to file: %s", json_file_path)
            return json_file_path

        except Exception as e:
            logging.error(
                "Error in prov_to_json: %s\nType: %s\nTraceback:\n%s",
                str(e),
                type(e).__name__,
                traceback.format_exc()
            )
            try:
                logging.error("prov_json content: %r", prov_json)
            except Exception:
                logging.error("prov_json not available or failed to log.")
            print(f"Error: {e} ")
            traceback.print_exc()
            return None

    # To convert values
    @staticmethod
    def convert_value(value):
        if isinstance(value, float) or isinstance(value, int):
            return str(value)
        elif isinstance(value, list):
            return [Workflow.convert_value(v) for v in value]
        elif isinstance(value, dict):
            return {k: Workflow.convert_value(v) for k, v in value.items()}
        return value  # Return as is if it's already serializable
