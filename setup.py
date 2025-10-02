#!/usr/bin/env python3
"""
Setup script for FIRST® MatchBox™
"""

from setuptools import setup

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

with open("requirements.txt", "r", encoding="utf-8") as fh:
    requirements = [line.strip() for line in fh if line.strip() and not line.startswith("#")]

_ = setup(
    name="first-matchbox",
    version="1.0.0",
    author="FTC Community",
    description="FIRST® MatchBox™",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/your-org/matchbox",
    py_modules=["matchbox"],
    scripts=["matchbox-cli.py"],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Education",
        "Topic :: Multimedia :: Video",
        "Topic :: Education",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
    python_requires=">=3.8",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "matchbox=matchbox:main",
            "matchbox-cli=matchbox-cli:main",
        ],
    },
    keywords="ftc first robotics obs streaming video autosplit websocket",
    project_urls={
        "Documentation": "https://github.com/FTCTeam10298/MatchBox/blob/main/README.md",
        "Source": "https://github.com/FTCTeam10298/MatchBox",
        "Tracker": "https://github.com/FTCTeam10298/MatchBox/issues",
    },
)