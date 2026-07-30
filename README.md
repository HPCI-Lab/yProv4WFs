# yProv4WFs
yProv4WFs is a service to track the provenance of a workflow run via a Workflow Management System (WMS) at run time. It is compliant with the W3C PROV standard.

It allows scientists or users in general to manage the provenance information collected during the execution. It focuses on a whole workflow and on specific steps inside it.

yProv4WFs is a [University of Trento](https://www.unitn.it) project, that extends the [yProv service](https://github.com/HPCI-Lab/yProv) moving the attention on the workflow level. It is designed as a plug-in usable by various WMS.

## Current supports
yProv4WFs is developed to be run on the following Workflow Management Systems:
-  [Streamflow](https://github.com/HPCI-Lab/yProv4WFs/blob/main/yprov4wfs/yProv4WFs_Streamflow/HowToRun_yProv4WFs_Streamflow.md)
<!---
-  ecFlow (
-->
## Installation

First, clone the repository and create the environment:
```bash
git clone https://github.com/HPCI-Lab/yProv4WFs.git
cd yProv4WFS
git checkout streamflow/sub-workflows-mapping
pip install -e . 
pip install streamflow==0.2.0.dev12
```

## Execution modes

yProv4WFs can operate in two distinct modes depending on your tracking requirements:

1. **Offline mode**: Runs the workflow natively with zero tracking overhead, and generates the provenance metadata explicitly from the internal database *after* the run finishes.

2. **Online mode**: Intercepts the core scheduler execution to dynamically capture data metrics on the fly, automatically bundling a zipped PROV-JSON package exactly when the workflow completes.

### Offline version
To enable StreamFlow to use yProv4WFs, modify the core file `streamflow/provenance/__init__.py` as follows:

```python
from yprov4wfs.yProv4WFs_Streamflow.yprov4wfs_Streamflow_fromdb import yProv4WFsProvenanceManager

prov_classes = {"run_crate": {"cwl": yProv4WFsProvenanceManager}}
```

Once these steps are completed, workflows can be executed using the standard StreamFlow commands:

```bash
streamflow run <workflow-file>
```

and the provenance can be generated with:

```bash
streamflow prov <workflow-file>
```

### Online version
The online tracking plugin acts as a progressive execution engine. To integrate it, replace the contents of StreamFlow's core scheduler file `streamflow/workflow/executor.py` with the code of `yprov4wfs_Streamflow.py`.

Once you have copied the code into Streamflow codebase, it is possible still to decide if to use the plugin or not:

- **By default**, running a workflow will use the **original scheduler**.
    ```bash
    streamflow run <workflow-file>
    ```
    or even

    ```bash
    USE_YPROV=false streamflow run <workflow-file>
    ```
- To **activate the online provenance** tracking on the fly, run your workflow with the `USE_YPROV` environment flag set to `true`:
    ```bash
    USE_YPROV=true streamflow run <workflow-file>
    ```

#### Batch configuration
The runtime version includes a batch strategy in order to reduce the overhead of IO operations.
Within the `yprov4wfs_Streamflow.py` file, you can find two parameters:

```python
_FLUSH_BATCH_SIZE = 10 # tasks
_FLUSH_MIN_INTERVAL_S = 5.0 * 60.0 # minutes
```

These are the default values but can be changed based on the requirements.

- **_FLUSH_BATCH_SIZE**: the number of tasks to be completed before executing a flush.

- **_FLUSH_MIN_INTERVAL_S**: the number of seconds that need to elapse before executing a flush unless the _FLUSH_BATCH_SIZE has been reached.