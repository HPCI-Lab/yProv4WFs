from setuptools import setup, find_packages

setup(
    name='yprov4wfs',                    
    version='0.0.4.3',                     
    packages=find_packages(include=["yprov4wfs", "yprov4wfs.*"]), 
    include_package_data=True,           
    install_requires=[],
    author='Carolina Sopranzetti',                 
    description='A module for tracking the provenance of a workflow using a Workflow Management System.',  
    long_description=open('README.md').read(), 
    long_description_content_type='text/markdown',  
    url='https://github.com/HPCI-Lab/yProv4WFs',
    license='GNU General Public License v3 (GPLv3)',  
    classifiers=[                        
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: GNU General Public License v3 (GPLv3)',  
        'Operating System :: OS Independent',      
    ],
    python_requires='>=3.6',
    maintainer='HPCI Lab - University of Trento',             
)
