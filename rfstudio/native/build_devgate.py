# -*- coding: utf-8 -*-
"""Build rfs_native (the compiled deviation gate) and place the .pyd next to
this script.

Usage (from the repo root or this directory):
    python rfstudio\\native\\build_devgate.py          # --clean optional

Requires on the machine:
  * an MSVC toolchain (VS Installer's "Desktop development with C++") --
    located via vswhere, any recent VS/BuildTools with VC.Tools.x86.x64;
  * pip install pybind11        (into whatever Python builds the module);
  * that Python must match FreeCAD's minor version (3.11 for FreeCAD 1.1)
    so the pyd's ABI tag loads in freecadcmd -- the build names the output
    rfs_native.<pyver>-win_amd64.pyd accordingly.

The workbench never REQUIRES the module: patches._deviation_of_curves falls
back to its pure-Python distToShape loop when the import fails (wrong abi,
missing build, non-Windows, ...).
"""

import os
import subprocess
import sys
import sysconfig


def fail(msg):
    print("BUILD FAILED: %s" % msg)
    sys.exit(1)


def vswhere_vcroot():
    candidates = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ),
                     "Microsoft Visual Studio", "Installer", "vswhere.exe"),
        os.path.join(os.environ.get("ProgramFiles", ),
                     "Microsoft Visual Studio", "Installer", "vswhere.exe"),
    ]
    for exe in candidates:
        if exe and os.path.exists(exe):
            try:
                out = subprocess.run(
                    [exe, "-latest", "-products", "*",
                     "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                     "-property", "installationPath"],
                    capture_output=True, text=True)
                path = (out.stdout or "").strip().splitlines()
                if path and path[0].strip():
                    return path[0].strip()
            except OSError:
                pass
    # Fallback: scan the usual install roots for any VC toolset.
    for root in ("C:\\Program Files\\Microsoft Visual Studio",
                 "C:\\Program Files (x86)\\Microsoft Visual Studio"):
        if not os.path.isdir(root):
            continue
        for vs in os.listdir(root):
            vcvars = os.path.join(root, vs, "Community", "VC",
                                  "Auxiliary", "Build", "vcvars64.bat")
            if os.path.exists(vcvars):
                return os.path.join(root, vs, "Community")
    return None


def find_tools(root):
    msvc = os.path.join(root, "VC", "Tools", "MSVC")
    versions = sorted(os.listdir(msvc)) if os.path.isdir(msvc) else []
    for ver in reversed(versions):
        cl = os.path.join(msvc, ver, "bin", "Hostx64", "x64", "cl.exe")
        if os.path.exists(cl):
            return cl, os.path.join(root, "VC", "Auxiliary", "Build",
                                    "vcvars64.bat")
    return None, None


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, "rfs_native.cpp")
    if not os.path.exists(src):
        fail("missing %s" % src)

    root = vswhere_vcroot()
    if root is None:
        fail("no MSVC installation found (install VS Build Tools with the "
             "C++ workload)")
    cl, vcvars = find_tools(root)
    if cl is None:
        fail("no cl.exe under %s\\VC\\Tools\\MSVC" % root)
    print("MSVC: %s" % os.path.dirname(os.path.dirname(os.path.dirname(cl))))

    inc_py = sysconfig.get_paths()["include"]
    lib_py = os.path.join(sys.base_prefix, "libs")
    if not os.path.isdir(lib_py):
        fail("python import library dir missing: %s" % lib_py)
    try:
        import pybind11
        inc_pb = pybind11.get_include()
    except ImportError:
        fail("pybind11 not installed in this Python: python -m pip install "
             "pybind11")
    print("python %s pyd -> rfs_native copy of include %s"
          % (sys.version.split()[0], inc_py))

    ext = sys.platform == "win32" and ".pyd" or ".so"
    # FreeCAD loads the ABI-tagged name first; plain rfs_native.pyd works on
    # Windows too (importlib checks the plain suffix as well), but the tagged
    # name documents the supported build explicitly.
    tag = sysconfig.get_config_var("EXT_SUFFIX") or "cp311-win_amd64.pyd"
    out = os.path.join(here, "rfs_native%s" % (tag if tag != ext else ext))
    obj = os.path.join(here, "rfs_native.obj")

    data_files = [src]
    cmd = ("call \"%s\"\n"
           "cl /nologo /std:c++17 /EHsc /O2 /MD /W3 \"%s\" "
           "/I\"%s\" /I\"%s\" /c /Fo\"%s\"\n"
           "link /nologo /DLL /OUT:\"%s\" \"%s\" "
           "/LIBPATH:\"%s\" python311.lib /MANIFEST:NO"
           % (vcvars, data_files[0], inc_pb, inc_py, obj,
              out, obj, lib_py))
    if not os.path.exists(os.path.basename(out)) or os.environ.get("CLEAN"):
        pass
    script = os.path.join(here, "_build.bat")
    with open(script, "w", encoding="ascii") as fh:
        fh.write("@echo off\r\n" + cmd + "\r\n")
    proc = subprocess.run(["cmd", "/c", script], capture_output=True,
                          text=True, errors="replace", cwd=here)
    print(proc.stdout[-6000:])
    if proc.returncode != 0 or not os.path.exists(out):
        print((proc.stderr or "")[-4000:])
        fail("compiler/linker step failed (see output above)")
    try:
        os.remove(obj)
        os.remove(script)
    except OSError:
        pass
    print("OK %s (%d bytes)" % (out, os.path.getsize(out)))
    print("run a freecadcmd geometry check next: the suite in test_core.py "
          "loads it automatically when present.")


if __name__ == "__main__":
    main()
