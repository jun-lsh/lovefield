## What Is This?

This is a fork of [GEF](https://github.com/hugsy/gef) that includes three major improvements:

1. Adds heuristic commands for kernel debugging **without requiring a symbolized `vmlinux`** (for `qemu-system`, supports Linux kernel 3.x-7.0.x).
2. Expands support to [many architectures](docs/QEMU-USER-SUPPORTED-ARCH.md) (for `qemu-user`).
3. Provides heap dump commands for multiple memory allocators.

Numerous other commands have been added and enhanced. Enjoy!

### Qemu-system Cooperation

- `pagewalk`: dumps page tables.
- `v2p`/`p2v`: displays the transformation between virtual addresses and physical addresses.
- `xp`: is a shortcut for physical memory dump.
- `qreg`: displays the register values from qemu-monitor (allows getting values like `$cs` even under qemu 2.x).
  - It is a shortcut for `monitor info registers`.
  - It also prints the details of each bit of the system register when x64/x86.

- `sysreg`: pretty prints system registers.
  - It shows `info registers` results, excluding general registers.

- `msr`: reads/writes MSR (Model Specific Registers) value by embedding/executing dynamic assembly.
  - Supported on x64 and x86.

- `cet`: displays Intel CET settings.
- `vbar`: displays ARM/ARM64 vector table.

- `kbase`: displays the kernel base address.
- `kversion`: displays the kernel version.
- `kcmdline`: displays the kernel cmdline used at boot time.
- `kcurrent`: displays current task address.

- `kvmmap`: prints kernel memory map.

- `ksymaddr-remote`: displays kallsyms information from scanning kernel memory.
  - Supported kernel versions: 3.x to 7.0.x.

- `ksymaddr-remote-apply`/`vmlinux-to-elf-apply`: applies kallsyms information obtained by `ksymaddr-remote` or `vmlinux-to-elf` to gdb.
  - Once you get a symboled pseudo ELF file, you can reuse and apply it automatically even after rebooting qemu-system.
  - `vmlinux-to-elf-apply` and `ksymaddr-remote-apply` provide almost the same functionality.
    - `vmlinux-to-elf-apply`: Requires installation of external tools. Create `vmlinux` with symbols.
    - `ksymaddr-remote-apply`: Requires no external tools. Create a blank ELF with embedded symbols only.

- `ktypes`: displays kernel type information from scanning kernel memory.

- `ktypes-load`: loads kernel type information from scanning kernel memory.

- `slub-dump`: dumps slub free-list.
  - Supported on x64/x86/ARM64/ARM + `SLUB` + no-symbol + kASLR.
  - Supported regardless of whether `CONFIG_SLAB_FREELIST_HARDENED` is `y` or `n`.
  - Supported regardless of whether `CONFIG_SLAB_VIRTUAL` is `y` or `n` (x64 only).
  - It supports dumping partial pages (`-v`) and NUMA node pages (`-vv`).
  - Since `page_to_virt` is difficult to implement, it will heuristically determine the virtual address from the free-list.

  - It supports `sheaf/barn` mechanism for linux 6.18~.

- `slab-dump`: dumps slab free-list.
  - Supported on x64/x86/ARM64/ARM + `SLAB` + no-symbol + kASLR.

- `slob-dump`: dumps slob free-list.
  - Supported on x64/x86/ARM64/ARM + `SLOB` + no-symbol + kASLR.

- `slub-tiny-dump`: dumps slub-tiny free-list.
  - Supported on x64/x86/ARM64/ARM + `SLUB-TINY` + no-symbol + kASLR.

- `slab-contains`: resolves the slab cache (`kmem_cache`) that a certain address (object) belongs to (for `SLUB`/`SLUB-TINY`/`SLAB`).
  - For `SLUB`/`SLUB-TINY`, if all chunks belonging to a certain `page` are in use, they will not be displayed by `slub-dump`/`slub-tiny-dump` command.
  - Even with such an address (object), this command may be able to resolve `kmem_cache`.

- `kmem-cache-alias`: dumps `kmem_cache` alias name.

- `buddy-dump`: dumps the zone of the page allocator (buddy allocator) free-list.

- `vmalloc-dump`: dumps `vmalloc` used-list and freed-list.

- `page`: displays the transformation between a `struct page` and its virtual/physical address.
  - There are shortcuts: `virt2page`, `page2virt`, `phys2page` and `page2phys`.

- `slab-virtual`: displays the transformation between slab-meta and its slab-data/`struct page` address (for `CONFIG_SLAB_VIRTUAL=y`).

- `pageinfo`: dumps `struct page->{flags,page_type}`.

- `highmem-dump`: dumps `HighMem` mappings.

- `kchecksec`: checks kernel security.

- `kmagic`: displays useful addresses in the kernel.

- `kconfig`: dumps the kernel config if available.

- `syscall-table-view`: displays the system call table.
  - It also dumps the ia32/x32 syscall table under x64.
  - It also dumps the compat syscall table under ARM64.

- `ksysctl`: dumps the sysctl parameters.

- `ktask`: displays each task's address.
  - It also displays the memory map of the userland process.

  - It also displays the register values saved on the kstack of the userland process.

  - It also displays the file descriptors of the userland process.

  - It also displays the signal handlers of the userland process.

  - It also displays the namespaces of the userland process.

  - It also displays the seccomp-filter.

- `kmod`: displays each module's address.
  - It also displays the symbols of each module.

- `kload`: loads `vmlinux` without a load address.
  - It is useful if you have a `vmlinux` with `debuginfo` at hand.
- `kmod-load`: loads the kernel module without a load address.
  - It is useful if you have a kernel module with `debuginfo` at hand.
- `kops`: displays each operation's member.

- `kcdev`: displays information for each character device.

- `kbdev`: displays information for each block device.
  - If there are too many block devices, detection will not be successful.
  - This is because block devices are not managed in one place, so I use the list of `bdev_cache` obtained from the slub-dump results.

- `kfilesystems`: dumps supported file systems.

- `kclock-source`: dumps the clocksource list.

- `kdmesg`: dumps the ring buffer of the dmesg area.

- `kpipe`: displays information for each pipe.

- `kbpf`: dumps the BPF information.

- `ktimer`: dumps the timer.

- `kpcidev`: dumps the PCI devices.

- `kipcs`: dumps IPCs information (System V semaphore, message queue and shared memory).

- `kdevio`: dumps I/O-port and I/O-memory information.

- `kdmabuf`: dumps DMA-BUF information.

- `kirq`: dumps irq information.

- `knetdev`: displays net devices.

- `ksearch-code-ptr`: searches for the code pointer in kernel data area.

- `thunk-tracer`: collects and displays the thunk function addresses that are called automatically (x64/x86 only).
  - If this address comes from RW area, this is useful for getting RIP.

- `usermodehelper-tracer`: collects and displays the information that is executed by `call_usermodehelper_setup`.

- `kmalloc-tracer`: collects and displays information when `kmalloc`/`kfree`.

- `kmalloc-allocated-by`: calls a predefined set of system calls and prints structures allocated by `kmalloc` or freed by `kfree`.

- `ktrace`: traces kernel functions and arguments.

- `xsm`: dumps secure memory when gdb is in normal world.
  - Supported on ARM64 and ARM.

- `wsm`: writes the value to secure memory when gdb is in normal world.
  - Supported on ARM64 and ARM.

- `bsm`: sets the breakpoint to secure memory when gdb is in normal world.
  - Supported on ARM64 and ARM.

- `optee-break-ta`: sets the breakpoint to the offset of OPTEE-Trusted-App when gdb is in normal world.
  - Supported on ARM64 and ARM.

- `optee-smc-service-dump`: dumps OPTEE SMC services.
  - Supported on ARM64.

- `optee-ta-dump`: dumps the information of OPTEE-Trusted-Apps from the memory or specified host directory.
  - Supported on ARM64 and ARM.

- `optee-shm-list`: shows the information of dynamic shared-memory buffers.
  - Supported on ARM64 and ARM.

- `pac-keys`: pretty prints ARM64 PAC keys.
  - Supported on ARM64.

- `uefi-ovmf-info`: dumps addresses of some important structures in each boot phase of UEFI when OVMF is used.
  - Supported on x64.

- `qemu-device-info`: dumps device information for qemu-escape.

### Added Features

- `pid`/`tid`: prints pid and tid.
- `filename`: prints filename.
- `fds`: shows opened file descriptors.
- `auxv`: pretty prints ELF auxiliary vector.
  - Supported also under `qemu-user`.

- `argv`/`envp`: pretty prints argv and envp.

- `dumpargs`: dumps arguments of current function.

- `vdso`: disassembles the text area of vdso smartly.

- `vvar`: dumps the area of vvar.
  - This area is mapped to userland, but cannot be accessed from gdb.
  - Therefore, it executes the assembly code and retrieves the contents.

- `gdtinfo`: pretty prints GDT entries. If userland, show sample entries.

- `idtinfo`: pretty prints IDT entries. If userland, show sample entries.

- `tls`: pretty prints TLS area. Requires glibc.

- `fsbase`/`gsbase`: pretty prints `$fs_base`, `$gs_base`.

- `libc`/`ld`/`heapbase`/`codebase`: displays each of the base address.

- `got-all`: shows got entries for all libraries.
- `break-rva`: sets a breakpoint at relative offset from codebase.

- `command-break`: sets a breakpoint which executes user defined command if hit.

- `main-break`: sets a breakpoint at `main` with or without symbols, then continue.
  - This is useful when you just want to run to `main` using `qemu-user` or `pin`, or debugging no-symbol ELF.
- `load-break`: breaks if something is loaded.
- `regdump-break`: sets a breakpoint which dumps specified registers if hit.
- `multi-break`: sets multiple breakpoints easily.
- `break-if-taken`/`break-if-not-taken`: sets a breakpoint which breaks if branch is taken (or not taken).
- `distance`: calculates the offset from its base address.

- `fpu`/`mmx`/`sse`/`avx`/`avx512`: pretty prints FPU/MMX/SSE/AVX/AVX512 registers.

- `xmmset`: sets the value to xmm/ymm/zmm register simply.

- `mmxset`: sets the value to mm register simply.

- `exec-until`: executes until specified operation.
  - Supports the following patterns:
    - call
    - jmp
    - syscall
    - ret
    - indirect-branch (x64/x86 only)
    - all-branch (call || jmp || ret)
    - memory-access (detect just `[...]`)
    - specified-keyword-regex
    - specified-condition (expressions using register or memory values)
    - user-code
    - libc-code
    - secure-world
    - region-change

- `call-trace`: traces call, ret, and syscall instructions.

- `xuntil`: executes until specified address.
  - It is slightly easier to use than the original until command.
- `add-symbol-temporary`: adds symbol information from command-line.

- `errno`: displays errno list or specified errno.

- `u2d`: shows cast/convert u64 <-> double/float.

- `unsigned`: shows unsigned value.

- `convert`: shows various conversion.

- `addressify`: converts reverse-order hex values to address.

- `walk-link-list`: walks the link list.

- `hexdump-flexible`: displays the hexdump with user defined format.

- `hash`: calculates various (450+) hashes, or show known-collisions.

- `crc`: calculates various CRCs.

- `json`: pretty prints json.

- `base-n-decode`/`base-n-encode`: decodes/encodes various baseN.

- `morse-decode`/`morse-encode`: decodes/encodes morse code.

- `saveo`/`diffo`: saves and diffs the command outputs.

- `memcmp`: compares the contents of the address A and B, whether virtual or physical.

- `memset`: sets the value to the memory range, whether virtual or physical.
- `memcpy`: copies the contents from the address A to B, whether virtual or physical.
- `memswap`: swaps the contents of the address A and B, whether virtual or physical.
- `meminsert`: inserts the contents of the address A to B, whether virtual or physical.

- `strlen`: detects the length of the string.

- `is-mem-zero`: checks the contents of address range are all 0x00 or 0xff.

- `seq-length`: detects consecutive length of the same sequence.

- `strings`: searches for ASCII string from specific location.

- `xs`: dumps string like `x/s` command, but with hex-string style.

- `xc`: dumps address like `x/x` command, but with coloring at some intervals.

- `ii`: is a shortcut for `x/50i $pc` with opcode bytes.
  - It prints the value if it is memory access operation.

- `extra`: manages user specified command to execute when each step.
- `comment`: manages user specified temporary comment.
- `seccomp`: invokes `ceccomp` or `seccomp-tools`.
- `onegadget`: invokes `one_gadget`.

- `rp`: invokes `rp++` with commonly used options.
- `call-syscall`: calls system call with specified values.

- `mmap`: allocates a new memory by `call-syscall`.
- `munmap`: unmaps a memory by `call-syscall`.
- `killthreads`: kills specific or all threads (for `pthread`).
- `constgrep`: invokes `grep` under `/usr/include/`.

- `proc-dump`: dumps each file under `/proc/PID/`.

- `up`/`down`: are wrappers for native `up`/`down`.
  - It shows also backtrace.
- `time`: measures the time of the GDB command.

- `multi-line`: executes multiple GDB commands in sequence.

- `cpuid`: shows the result of cpuid(eax=0,1,2...).

- `read-system-register-for-qemu-arm`: reads system register for old `qemu-system-arm`.
- `read-system-register-for-kgdb`: reads system register for kgdb (x64/ARM64 only).
- `capability`: shows the capabilities of the debugging process.

- `dasm`: disassembles the code by capstone.

- `asm-list`: lists instructions. (x64/x86 only)
  - This command uses x86data.js from https://github.com/asmjit/asmdb

- `syscall-search`: searches for system call by regex.

- `dwarf-exception-handler`: dumps the DWARF exception handler information.

- `magic`: displays useful addresses in glibc etc.

- `dynamic`: dumps the `_DYNAMIC` area.

- `link-map`: dumps useful members of `link_map` with iterating.

- `dtor-dump`: dumps some destructor functions list.

- `ptr-mangle`: shows the mangled value that will be mangled by `PTR_MANGLE`.
- `ptr-demangle`: shows the demangled value of the value mangled by `PTR_MANGLE`.

- `search-mangled-ptr`: searches for the mangled value from RW memory.

- `follow`: changes `follow-fork-mode` setting.

- `smart-cpp-function-name`: toggles `context.smart_cpp_function_name` setting.
- `ret2dl-hint`: shows the structure used by return-to-dl-resolve as hint.

- `srop-hint`: shows the code for sigreturn-oriented-programming as hint.

- `sigreturn`: displays stack values for sigreturn syscall.

- `smart-memory-dump`: dumps all regions of the memory to each file.

- `load-file`: loads the file into memory.
- `load-file-mmap`: loads the file into memory that allocated by `mmap`.
- `search-cfi-gadgets`: searches for CFI-valid (for CET IBT) and controllable generally gadgets in the executable area.

- `symbols`: lists all symbols with coloring.

- `types`: lists all types with compaction.

- `dt`: makes it easier to use `ptype /ox TYPE` and `p ((TYPE*) ADDRESS)[0]`.
  - This command is designed for several purposes.
    1. When displaying very large struct, you may want to go through a pager because the results will not fit on one screen.
       However, using a pager, the color information disappears. This command calls the pager with preserving colors.
    2. When `ptype /ox TYPE`, interpreting member type recursively often result is too long and difficult to read.
       This command keeps result compact by displaying only top-level members.
    3. When `p ((TYPE*) ADDRESS)[0]` for large struct, the setting of `max-value-size` is too small to display.
       This command adjusts it automatically.
    4. When debugging a binary written in the Golang, the offset information of the type is not displayed.
       This command also displays the offset.
    5. When debugging a binary written in the Golang, the `p ((TYPE*) ADDRESS)[0]` command will be broken.
       This is because the Golang helper script is automatically loaded and overwrites the behavior of `p` command.
       This command creates the display results on the Python side, so we can display it without any problems.

- `mte-tags`: displays the MTE tags for the specified address.
  - Supported on ARM64.

- `iouring-dump`: dumps the area of iouring (x64 only).
  - This area is mapped to userland, but cannot be accessed from gdb.
  - Therefore, it executes the assembly code and retrieves the contents.

- `gef version`: shows software versions that GEF uses.

- `gef status`: shows architecture information used in GEF.

- `gef reset-breakpoint`: shows and resets all breakpoints.
- `gef arch-list`: displays defined architecture information.

- `gef pyobj-list`: displays defined global Python objects.

- `gef avail-comm-list`: displays a list of commands which are available or not for the current architecture and gdb execution mode.

- `gef set-arch`: sets a specific architecture to GEF.
- `gef check-update`: checks for GEF updates.
- `gef dump-commands`: dumps GEF command documentation as Markdown.
- `binwalk-memory`: scans memory by `binwalk`.

- `filetype-memory`: scans memory by `file` and `magika`.

- `sixel-memory`: shows image to terminal by `imagemagick`.
  - If you have `pillow` and `pyzbar` installed, a barcode detection option is also available.

- `stdio-dump`: dumps members of stdin/stdout/stderr.

- `peek-pageframe`: reads page frame data.

- `peek-pageflags`: reads page flags of a page frame.

- `angr`: finds simple constraints by `angr`.

- `history`: shows gdb command history easily.

- `crc32rev`: performs CRC32 reverse calculation limited to ASCII character range.

- `vdump`: visualizes memory data like an image.

- `freq-analysis`: visualizes the frequency of occurrence of each byte.

- `qemu-system-memory-region-dump`: dumps memory regions for `qemu-system`.

- `find-syscall`: searches the syscall gadget.

- `fpchain`: dumps chains from `__IO_list_all`.

- `stepi-for-kgdb`: is wrapper for AArch64 KGDB that avoids stepping into pending IRQ handlers.
- `xskip`: skips instructions easily.
- `xtap`: taps read/write syscalls on specific file descriptors and hexdump the transferred data.
