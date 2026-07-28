#!/bin/bash
# Build bwa-mem2 from source for arm64 Linux using sse2neon.
#
# This is the same technique the Homebrew formula (brewsci/bio/bwa-mem2) uses
# for macOS ARM, adapted for Linux arm64 containers. sse2neon translates Intel
# SSE/AVX intrinsics to ARM NEON at compile time.
#
# Called from the Dockerfile's arm64 branch. Expects:
#   - BWA_MEM2_VERSION env var (e.g. "2.3")
#   - Build tools already installed: git, cmake, dos2unix, build-essential, zlib1g-dev
#   - Patches downloaded to /tmp/fastmap.patch and /tmp/bandedSWA.patch
set -euo pipefail

cd /tmp/bwa-mem2

# --- Normalize line endings on all source files we'll patch ---
# bwa-mem2 ships with CRLF line endings on some files, which can interfere
# with sed and patch. Convert all .h and .cpp files we'll modify.
dos2unix src/bandedSWA.h src/bandedSWA.cpp src/fastmap.cpp src/FMI_search.h \
         src/kswv.h src/ksw.h src/ksw.cpp src/bwamem.cpp src/main.cpp \
         src/sse2neon.h 2>/dev/null || true

# --- sse2neon: translates Intel SSE/AVX intrinsics to ARM NEON ---
cp src/sse2neon.h src/sse2neon.h.bak 2>/dev/null || true

# Fix _rdtsc name: sse2neon defines _rdtsc (single underscore), but bwa-mem2
# calls __rdtsc (double underscore) throughout. Rename the definition to match.
sed -i 's/FORCE_INLINE uint64_t _rdtsc(void)/FORCE_INLINE uint64_t __rdtsc(void)/' \
    src/sse2neon.h

# Replace Intel intrinsics headers with sse2neon.h in all source files that
# include them. immintrin.h is the umbrella header; emmintrin.h is SSE2;
# smmintrin.h is SSE4.1.
for f in src/FMI_search.h src/kswv.h src/bandedSWA.h; do
    sed -i 's/#include <immintrin.h>/#include "sse2neon.h"/' "$f"
done
for f in src/ksw.h src/ksw.cpp; do
    sed -i 's/#include <emmintrin.h>/#include "sse2neon.h"/' "$f"
done
# bandedSWA.h has a conditional include block:
#   #if (__AVX512BW__ || __AVX2__)
#       #include <immintrin.h>
#   #else
#       #include <smmintrin.h>
#       #define __mmask8 uint8_t
#       #define __mmask16 uint16_t
#   #endif
# On arm64 neither __AVX512BW__ nor __AVX2__ is defined, so the #else branch
# is taken. We need sse2neon.h included regardless of which branch runs, and
# the __mmask8/__mmask16 defines from the #else branch. Replace the whole
# block with sse2neon.h + the type defines.
# Using python for the multiline replace because sed -c on GNU/Linux handles
# \n in replacements but we need this to be portable and reliable.
python3 -c "
import re, pathlib
p = pathlib.Path('src/bandedSWA.h')
text = p.read_text()
text = re.sub(
    r'#if \(__AVX512BW__ \|\| __AVX2__\).*?#endif',
    '#include \"sse2neon.h\"\n#define __mmask8 uint8_t\n#define __mmask16 uint16_t',
    text, flags=re.DOTALL)
p.write_text(text)
"

# --- Fix _mm_prefetch calls in FMI_search.cpp ---
# sse2neon's _mm_prefetch takes char const *, but FMI_search.cpp passes
# uint8_t* pointers without a cast. On x86 this works via implicit conversion
# through immintrin.h's macro; on arm64 the types don't match without an
# explicit cast.
#
# Pattern A: _mm_prefetch((const char *)(&ptr), ...) — already has a C-style
# cast. Replace the C-style cast with reinterpret_cast, consuming both the
# cast's closing ) and the inner grouping's opening (.
sed -i 's/_mm_prefetch((const char \*)(&/_mm_prefetch(reinterpret_cast<const char*>(\&/g' \
    src/FMI_search.cpp

# Pattern B: _mm_prefetch(&array[idx], ...) — no cast at all. Wrap the address
# in reinterpret_cast<const char*>(...), adding the opening and closing parens.
sed -i 's/_mm_prefetch(&sa_ls_word\[pos >> SA_COMPX\]/_mm_prefetch(reinterpret_cast<const char*>(\&sa_ls_word[pos >> SA_COMPX])/g' \
    src/FMI_search.cpp
sed -i 's/_mm_prefetch(&sa_ms_byte\[pos >> SA_COMPX\]/_mm_prefetch(reinterpret_cast<const char*>(\&sa_ms_byte[pos >> SA_COMPX])/g' \
    src/FMI_search.cpp
sed -i 's/_mm_prefetch(&sa_ls_word\[sp >> SA_COMPX\]/_mm_prefetch(reinterpret_cast<const char*>(\&sa_ls_word[sp >> SA_COMPX])/g' \
    src/FMI_search.cpp
sed -i 's/_mm_prefetch(&sa_ms_byte\[sp >> SA_COMPX\]/_mm_prefetch(reinterpret_cast<const char*>(\&sa_ms_byte[sp >> SA_COMPX])/g' \
    src/FMI_search.cpp
sed -i 's/_mm_prefetch(&cp_occ\[occ_id_pp_\]/_mm_prefetch(reinterpret_cast<const char*>(\&cp_occ[occ_id_pp_])/g' \
    src/FMI_search.cpp

# --- Fix AVX512 code in bwamem.cpp (no AVX512 on arm64) ---
# Replace AVX512 zero-init and store with SSE equivalents.
sed -i 's/__m512i zero512 = _mm512_setzero_si512()/__m128i zero128 = _mm_setzero_si128();/' \
    src/bwamem.cpp
sed -i 's/_mm512_store_si512((__m512i \*)(hist + i), zero512)/_mm_store_si128((__m128i *)(hist + i), zero128)/' \
    src/bwamem.cpp

# --- Apply community patches for non-x86 builds ---
# From the Homebrew formula: fix __cpuid for non-x86 and fix SSE4.1
# _mm_blendv_epi8 redefinition.
patch -p1 < /tmp/fastmap.patch
patch -p1 < /tmp/bandedSWA.patch

# --- Fix version string (upstream v2.3 tag forgot to bump it from 2.2.1) ---
sed -i 's/#define PACKAGE_VERSION "2\.2\.1"/#define PACKAGE_VERSION "2.3"/' \
    src/main.cpp

# --- Build safestringlib v1.2.0 (replaces the bundled submodule) ---
# The bundled safestringlib in ext/ has issues with newer compilers and CMake
# policy versions. Build v1.2.0 separately and point the Makefile at it.
# safestringlib-1.2.0 is extracted to /tmp (not inside /tmp/bwa-mem2), so the
# Makefile paths must be absolute.
cd /tmp/safestringlib-1.2.0
# Remove hardening flags that fail on some linkers
sed -i 's| -z noexecstack -z relro -z now||' CMakeLists.txt
sed -i 's|LDFLAGS=-z noexecstack -z relo -z now|LDFLAGS=|' makefile
# Fix conflict with glibc's memset_s declaration
sed -i 's|extern errno_t memset_s|//xxx extern errno_t memset_s|' \
    include/safe_mem_lib.h
# CMake 4.x rejects cmake_minimum_required < 3.5
cmake -S . -B build \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release

# --- Update Makefile to use the separately-built safestringlib ---
cd /tmp/bwa-mem2
sed -i 's|SAFE_STR_LIB= ext/safestringlib/libsafestring\.a|SAFE_STR_LIB= /tmp/safestringlib-1.2.0/build/libsafestring_static.a|' \
    Makefile
sed -i 's|-Iext/safestringlib/include|-I/tmp/safestringlib-1.2.0/include|' \
    Makefile
sed -i 's|-Lext/safestringlib -lsafestring|-L/tmp/safestringlib-1.2.0/build/ -lsafestring_static|' \
    Makefile
sed -i 's|-Lext/safestringlib/ -lsafestring|-L/tmp/safestringlib-1.2.0/build/ -lsafestring_static|' \
    Makefile
# The SAFE_STR_LIB build target (Makefile ~line 123-124) tries to build the
# bundled safestringlib in ext/safestringlib/, which doesn't exist (we built
# v1.2.0 separately). The rule has no prerequisites, so make always runs it
# even though the file exists. Replace the recipe with a no-op.
sed -i '/^$(SAFE_STR_LIB):/,$ { /^$(SAFE_STR_LIB):/! { /^$/!d } }' \
    Makefile
sed -i '/^$(SAFE_STR_LIB):/s/$/\n\t@true/' \
    Makefile

# --- Build (arch=native lets the compiler use the CPU's native NEON support) ---
make arch=native

# --- Install ---
cp bwa-mem2 /usr/local/bin/bwa-mem2
chmod +x /usr/local/bin/bwa-mem2
