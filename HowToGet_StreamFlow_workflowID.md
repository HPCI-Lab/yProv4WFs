Get the *workflow_id* by consulting the StreamFlow result printed to the terminal:
```bash
streamflow run streamflow.yml
```

Possible expected ouput:
```console
Resolved 'flow.cwl' to 'file:///code/examples/try1/flow.cwl'
2024-04-12 23:28:42.584 INFO     Processing workflow 300c7a78-e098-4f53-842a-594c244b58ee

...

2024-04-12 23:29:01.730 INFO     COMPLETED Workflow execution
{
    "final_output": {
        "basename": "output.txt",
        "checksum": "sha1$528b8a86d748d10f39707cd5719ae4bc434718cb",
        "class": "File",
        "dirname": "/code/examples/try1/d7e16288-e33e-40fa-8ed9-0e47a12fc44e",
        "location": "file:///code/examples/try1/d7e16288-e33e-40fa-8ed9-0e47a12fc44e/output.txt",
        "nameext": ".txt",
        "nameroot": "output",
        "path": "/code/examples/try1/d7e16288-e33e-40fa-8ed9-0e47a12fc44e/output.txt",
        "size": 99
    }
}
2024-04-12 23:29:01.735 INFO     UNDEPLOYING dc-mpi
2024-04-12 23:29:01.736 INFO     COMPLETED Undeployment of dc-mpi
```

The second line contains the *workflow_id*, in our example:
300c7a78-e098-4f53-842a-594c244b58ee


Possible expected output of the streamflow prov commmand:
```console
Successfully created PROV4WFS archive at /code/examples/try1/workflow_esempio/7838648e-a93d-4345-b937-bd2290dcbf0d.zip
```
