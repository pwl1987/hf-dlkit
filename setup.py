"""安装垫片 —— 兼容**老 setuptools**。

为什么需要它
------------
``pyproject.toml`` 的 ``[project]`` 表是 PEP 621，setuptools **61.0** 才支持。
而 Debian 11 / Ubuntu 20.04 自带 setuptools 59.6，pip 22.0.2 在这些系统上
构建时会得到一个名叫 ``UNKNOWN-0.0.0`` 的空包 —— 因为
``/usr/lib/python3/dist-packages`` 会**泄漏进 pip 的构建隔离环境并遮蔽**
隔离出来的新版 setuptools，后者根本没机会读到 ``[project]``。

（现象：``pip install .`` 显示 "Successfully installed UNKNOWN-0.0.0"，
装完 ``import hfdl`` 直接 ModuleNotFoundError。）

所以这里显式判断 setuptools 版本：

* **>= 61** —— setuptools 自己会读 ``pyproject.toml``，这里什么都不用做；
* **< 61** —— 这里手动把元数据交给 setup()。

元数据仍然只有一份来源：``pyproject.toml``（新版路径）。
老路径下的少量字段在这里重复了一遍，是为了让 Debian 11 / Ubuntu 20.04
能装上；两处的版本号在 ``pyproject.toml`` 里改时记得同步这里。
"""
from __future__ import annotations

import os

from setuptools import find_packages, setup

HERE = os.path.abspath(os.path.dirname(__file__))
MIN_SETUPTOOLS = (61, 0)


def _setuptools_version() -> tuple[int, ...]:
    try:
        from setuptools import __version__ as raw
    except Exception:  # pragma: no cover
        return ()
    parts: list[int] = []
    for chunk in raw.split(".")[:3]:
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _read_version() -> str:
    """从 pyproject.toml 里抠出版本号（老 setuptools 路径用）。"""
    path = os.path.join(HERE, "pyproject.toml")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("version") and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:  # pragma: no cover
        pass
    return "0.0.0"


if _setuptools_version() >= MIN_SETUPTOOLS:
    # 新 setuptools：让它自己读 pyproject.toml
    setup()
else:
    setup(
        name="hf-dlkit",
        version=_read_version(),
        description=(
            "面向大文件的 Hugging Face 下载器：切片并行、镜像端点回落、逐文件 sha256 校验"
        ),
        long_description=open(os.path.join(HERE, "README.md"), encoding="utf-8").read(),
        long_description_content_type="text/markdown",
        license="MIT",
        author="pwl1987",
        url="https://github.com/pwl1987/hf-dlkit",
        python_requires=">=3.9",
        package_dir={"": "src"},
        packages=find_packages(where="src"),
        package_data={"hfdl": ["py.typed"]},
        entry_points={"console_scripts": ["hfdl = hfdl.cli:main"]},
        classifiers=[
            "Development Status :: 4 - Beta",
            "Environment :: Console",
            "Intended Audience :: Developers",
            "License :: OSI Approved :: MIT License",
            "Operating System :: OS Independent",
            "Programming Language :: Python :: 3",
            "Topic :: Internet :: WWW/HTTP",
            "Topic :: Scientific/Engineering :: Artificial Intelligence",
        ],
    )
