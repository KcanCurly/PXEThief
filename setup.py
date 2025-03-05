from setuptools import setup, find_packages

setup(
    name="pxethief",
    version="1.0.0",
    author="KcanCurly",
    description="PXEThief is a set of tooling that implements attack paths discussed at the DEF CON 30 talk",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/KcanCurly/PXEThief",
    packages=find_packages(),
    install_requires=[
        "scapy>=2.4.5",
        "requests>=2.27.1",
        "requests-toolbelt>=0.9.1",
        "pycryptodome>=3.14.1",
        "lxml>=4.9.1",
        "tftpy==0.8.2",
        "certipy-ad==4.8.2",
        "asn1crypto==1.5.1",
        "cryptography==41.0.7",
        "pyasn1-modules==0.4.1"

    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Intended Audience :: Information Technology",
        "Topic :: Security",
    ],
    python_requires=">=3.8",
    entry_points={
        "console_scripts": [
            "pxethief=src.pxethief:main"
        ],
    },
)