## yProv4WFs
yProv4WFs is a service to track the provenance of a workflow run via a Workflow Management System (WMS) at run time. It is compliant with the W3C PROV standard.

It allows scientists or users in general to manage the provenance information collected during the execution. It focuses on a whole workflow and on specific steps inside it.

yProv4WFs is a [University of Trento](https://www.unitn.it) project, that extends the [yProv service](https://github.com/HPCI-Lab/yProv) moving the attention on the workflow level. It is designed as a plug-in usable by various WMS.

### Current Supports:
yProv4WFs is developed to be run on the following Workflow Management Systems:
-  [Streamflow](https://github.com/HPCI-Lab/yProv4WFs/blob/main/yprov4wfs/yProv4WFs_Streamflow/HowToRun_yProv4WFs_Streamflow.md)
-  [Cylc](https://github.com/HPCI-Lab/yProv4WFs/blob/main/yprov4wfs/yProv4WFs_cylc/HowToRun_yProv4WFs_Cylc.md)
<!---
-  ecFlow (
-->
### Installation 🛠️:

First, clone the repository and create the environment:
```bash
git clone https://github.com/HPCI-Lab/yProv4WFs.git
cd yProv4WFS
git checkout streamflow/sub-workflows-mapping
pip install -e . 
pip install streamflow==0.2.0.dev12
```

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

> [!WARNING]
> Run `streamflow prov` from the directory containing all workflow and sub-workflow CWL definition files. yProv4WFs relies on StreamFlow being able to resolve the complete workflow structure, including referenced workflows, sub-workflows, and other CWL files. Executing the command from a different directory may prevent the provenance generation from completing successfully.
> 
> Otherwise, a warning like this is generated: `<date + time> WARNING  YPROV: No CWL file found in the current directory`.