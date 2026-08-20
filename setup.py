from pathlib import Path

from setuptools import setup, find_packages

# Read as UTF-8 rather than in whatever the locale encoding happens to be: the
# README contains emoji, which cp1252 (the Windows default) cannot decode, and
# the failure aborts metadata generation for the whole package. Resolving the
# path against this file also keeps it readable when setup.py runs from another
# working directory.
long_description = (Path(__file__).parent / 'README.md').read_text(encoding='utf-8')

setup(
    name='yprov4wfs',                    
    version='0.0.9',                     
    packages=find_packages(include=["yprov4wfs", "yprov4wfs.*"]), 
    include_package_data=True,           
    install_requires=[],
    author='Carolina Sopranzetti',                 
    description='A module for tracking the provenance of a workflow using a Workflow Management System.',  
    long_description=long_description,
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
