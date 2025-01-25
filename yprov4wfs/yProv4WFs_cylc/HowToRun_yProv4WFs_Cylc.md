## yProv4WFs - Cylc

Necessary steps to work with yProv4WFs in Cylc:
<!--
- Clone Cylc repository (inside conda environment preferably):
-->
- Install Cylc (inside conda environment preferably):
  - using conda:
    1. create a directory and access it
      * ```bash
        mkdir <dir_name>
        cd <dir_name>
        ```
    2. create a conda environment and install pip
      * ```bash
        conda create -n cylc
        ```
        if needed specify Python version 
      * ```bash
        conda create -n cylc "python<=3.11"
        conda activate cylc
        conda install pip
        ```
    3. install cylc-flow via pip
      * ```bash
        pip install cylc-flow
        ```
    4. go to the location of python module
        * verify installation: 
         ```bash
         pip show cylc-flow
         ```
        The result should be similiar to this:
         ```bash
         Name: cylc-flow
         Version: 8.4.0
         Summary: A workflow engine
         for cycling systems
         Home-page: https://cylc.org/
         Author: Hilary Oliver Author-email:
         License: GPL
         Location: /<dir_path>/conda/envs/cylc/lib/python3.11/site-packages
         Requires: ansimarkup, async-timeout, colorama, graphene, importlib_metadata, jinja2, metomi-isodatetim e, packaging, promise, protobuf, psutil, pyzma, rx, urwid Required-by: cylc-rose
         ```
        * go to Location and find cylc directory:
         ```bash
         cd /<dir_path>/conda/envs/cylc/lib/python3.11/site-packages
         ls
         ```
        * enter cylc directory and continue by entering flow directory too:
          ```bash
          cd cylc/flow
          ```
    5. modify file scheduler_cli.py via text editor (es. nano, vim):
        ```bash
        nano scheduler_cli.py
        ```
       * Replace this import:
        ```bash
        from cylc.flow.scheduler import Scheduler, SchedulerError
        ```
       * With the following one:
        ```bash
        from yprov4wfs.yProv4WFs_cylc.yprov4wfs_Cylc import Scheduler, SchedulerError
        ```
  <!--   
    3. clone cylc-flow repository
      * ```bash
        git clone https://github.com/cylc/cylc-flow.git
        ```
    4. modify file /cylc-flow/cylc/flow/scheduler_cli.py
        Replace this import:
        ```bash
        from cylc.flow.scheduler import Scheduler, SchedulerError
        ```
        With the following one:
        ```bash
        from yprov4wfs.yProv4WFs_cylc.yprov4wfs_Cylc import Scheduler, SchedulerError
        ```
    5. in the cylc-flow directory 
        ```bash
        pip install .
        ```
  - outside conda environment [not recommented]
    1. In desired folder
        ```bash
        git clone https://github.com/cylc/cylc-flow.git
        ```
    2. the path where the git clone was done: replace [...] with [your own path]
        ```bash
        pip install -e /.../cylc-flow
        ```
    3. modify file /cylc-flow/cylc/flow/scheduler_cli.py
        Replace this import:
        ```bash
        from cylc.flow.scheduler import Scheduler, SchedulerError
        ```
        With the following one:
        ```bash
        from yprov4wfs.yProv4WFs_cylc.yprov4wfs_Cylc import Scheduler, SchedulerError
        ```
      -->
- Using yProv4WFs library (inside conda environment preferably):
  1. Installing using pip:
    * ```bash
      pip install yprov4wfs
      ```
    * verify installation: 
      ```bash
      pip show yprov4wfs
      ```
  2. Installing by cloning the repository:
    * ```bash
      git clone https://github.com/HPCI-Lab/yProv4WFs.git
      cd /path/to/your/project
      ```
    * ```bash
      pip install .
      ```
  3. Installing from a GitHub Repository:
    * ```bash
      pip install git+https://github.com/HPCI-Lab/yProv4WFs.git
      ```
    * verify installation: 
      ```bash
      pip show yprov4wfs
      ```

- Follow Cylc documentation for running your project https://github.com/cylc/cylc-flow/blob/master/README.md

- Create your workflow following Cylc instructions <br>
  (You can find some examples in the Cylc documentation: 
  https://cylc.github.io/cylc-doc/stable/html/workflow-design-guide/index.html)

- First run the workflow using
  ```bash
  cylc vip
  ```
  or any other command line, depending on the cylc/flow version you are using.<br>
  Wait till the workflow's execution is concluded.

- The expected output is provided in the *cylc-run* directory, under the *workflow_name/run_number* folder, where you can find the tracked provenance as a json file (yProv4WFs.json).
