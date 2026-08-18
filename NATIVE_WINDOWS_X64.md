# Windows x64 native artifact

`afc_kernels.dll` is the prebuilt Windows x64 backend distributed beside
`afc_native.py`. Its purpose is to make Fast, Balanced and Maximum use the C++
pipeline on an ordinary 64-bit Windows/Python installation even when the user
does not have a compiler on `PATH`.

## Provenance

- Source: `afc_native.cpp` at commit `048c2fc50a3be8c44c98b25cccea926fa6942ffd`
- Toolchain: w64devkit x64 GCC, static runtime build
- Build command:

  ```bat
  g++ -O3 -std=c++17 -shared -static -pthread afc_native.cpp -o afc_kernels.dll
  ```

- Size: 691,763 bytes
- SHA-256:

  ```text
  f00a7157b6b25b1a99d120f5c0d3b909c11d01e7c1677f62cb11b3654acfa27d
  ```

## Inspection

- PE architecture: `pei-x86-64`
- Imported DLLs: `KERNEL32.dll`, `msvcrt.dll`
- Exported AFC entry points:
  - `afc_compress`
  - `afc_compress_ex`
  - `afc_decompress`
  - `afc_free`

The static MinGW build does not require libstdc++, libgcc or winpthread DLLs on
the target machine.

## Runtime verification

Run:

```bat
python -m afc_native --diagnose
```

On 64-bit Windows/Python it must report:

```text
Library architecture  : 64-bit
Library load          : SUCCESS
Export afc_compress_ex: YES
Native call test      : SUCCESS
RESULT: C++ native backend is ACTIVE
```

`tests/test_app.py` additionally verifies that every preset is native-capable,
that Python and C++ produce byte-identical containers, and that both directions
round-trip exactly. The Python implementation remains the reference and safe
fallback.
