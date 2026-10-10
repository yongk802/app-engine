using System;
using System.Text;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.ComponentModel;

namespace AppEngineDesktop {
// The broker keeps the only Job Object handle. Its exit cleans up exactly this
// service's process tree. The GUI never owns this handle, so GUI exit is harmless.
public sealed class WindowsProcess : IDisposable {
    IntPtr job=IntPtr.Zero, process=IntPtr.Zero;
    public static string Quote(string text) {
        var quoted=new StringBuilder("\""); int slashes=0;
        foreach(char c in text) {
            if(c=='\\') { slashes++; continue; }
            if(c=='\"') quoted.Append('\\',slashes*2+1).Append(c);
            else quoted.Append('\\',slashes).Append(c);
            slashes=0;
        }
        return quoted.Append('\\',slashes*2).Append('"').ToString();
    }
    public WindowsProcess(Config config,string log) {
        IntPtr output=IntPtr.Zero, input=IntPtr.Zero, block=IntPtr.Zero;
        PROCESS_INFORMATION info=new PROCESS_INFORMATION();
        try {
            job=CreateJobObject(IntPtr.Zero,null); Require(job!=IntPtr.Zero);
            var limits=new EXTENDED_LIMIT(); limits.Basic.LimitFlags=0x2000; // KILL_ON_JOB_CLOSE
            Require(SetInformationJobObject(job,9,ref limits,(uint)Marshal.SizeOf(limits)));
            var security=new SECURITY_ATTRIBUTES {Length=Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES)),Inherit=true};
            output=CreateFile(log,0x40000000,3,ref security,4,0x80,IntPtr.Zero); Require(output!=new IntPtr(-1));
            SetFilePointer(output,0,IntPtr.Zero,2);
            input=CreateFile("NUL",0x80000000,3,ref security,3,0x80,IntPtr.Zero); Require(input!=new IntPtr(-1));
            var startup=new STARTUPINFO {Size=Marshal.SizeOf(typeof(STARTUPINFO)),Flags=0x100,StdOutput=output,StdError=output,StdInput=input};
            string environment=String.Join("\0",config.Environment.OrderBy(p=>p.Key,StringComparer.OrdinalIgnoreCase).Select(p=>p.Key+"="+p.Value))+"\0\0";
            block=Marshal.StringToHGlobalUni(environment);
            string command=Quote(config.Python)+" "+String.Join(" ",config.Arguments.Select(Quote));
            Require(CreateProcess(config.Python,new StringBuilder(command),IntPtr.Zero,IntPtr.Zero,true,0x08000404,block,config.Repository,ref startup,out info));
            process=info.Process;
            // Suspend until assigned, preventing startup children from escaping.
            Require(AssignProcessToJobObject(job,process));
            Require(ResumeThread(info.Thread)!=UInt32.MaxValue);
        } catch { if(info.Process!=IntPtr.Zero) TerminateProcess(info.Process,1); Dispose(); throw; }
        finally {
            if(info.Thread!=IntPtr.Zero) CloseHandle(info.Thread);
            if(output!=IntPtr.Zero && output!=new IntPtr(-1)) CloseHandle(output);
            if(input!=IntPtr.Zero && input!=new IntPtr(-1)) CloseHandle(input);
            if(block!=IntPtr.Zero) Marshal.FreeHGlobal(block);
        }
    }
    public void Wait() { WaitForSingleObject(process,UInt32.MaxValue); }
    public void Dispose() {
        if(job!=IntPtr.Zero) { CloseHandle(job); job=IntPtr.Zero; }
        if(process!=IntPtr.Zero) { CloseHandle(process); process=IntPtr.Zero; }
    }
    static void Require(bool ok) { if(!ok) throw new Win32Exception(Marshal.GetLastWin32Error()); }
    [StructLayout(LayoutKind.Sequential)] struct SECURITY_ATTRIBUTES {public int Length; public IntPtr Descriptor; [MarshalAs(UnmanagedType.Bool)] public bool Inherit;}
    [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct STARTUPINFO {
        public int Size; public string Reserved,Desktop,Title; public int X,Y,XSize,YSize,XChars,YChars,Fill,Flags;
        public short Show,ReservedSize; public IntPtr ReservedData,StdInput,StdOutput,StdError;
    }
    [StructLayout(LayoutKind.Sequential)] struct PROCESS_INFORMATION {public IntPtr Process,Thread;public uint ProcessId,ThreadId;}
    [StructLayout(LayoutKind.Sequential)] struct BASIC_LIMIT {
        public long ProcessTime,JobTime;public uint LimitFlags;public UIntPtr Minimum,Maximum;public uint ActiveProcessLimit;public UIntPtr Affinity;public uint Priority,Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct IO_COUNTERS {public ulong ReadOps,WriteOps,OtherOps,ReadBytes,WriteBytes,OtherBytes;}
    [StructLayout(LayoutKind.Sequential)] struct EXTENDED_LIMIT {public BASIC_LIMIT Basic;public IO_COUNTERS IO;public UIntPtr ProcessMemory,JobMemory,PeakProcessMemory,PeakJobMemory;}
    [DllImport("kernel32.dll",SetLastError=true,CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr attributes,string name);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int type,ref EXTENDED_LIMIT info,uint size);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll",SetLastError=true,CharSet=CharSet.Unicode)] static extern bool CreateProcess(string app,StringBuilder command,IntPtr pa,IntPtr ta,bool inherit,uint flags,IntPtr environment,string cwd,ref STARTUPINFO startup,out PROCESS_INFORMATION info);
    [DllImport("kernel32.dll",SetLastError=true,CharSet=CharSet.Unicode)] static extern IntPtr CreateFile(string name,uint access,uint share,ref SECURITY_ATTRIBUTES attributes,uint disposition,uint flags,IntPtr template);
    [DllImport("kernel32.dll",SetLastError=true)] static extern uint SetFilePointer(IntPtr file,int distance,IntPtr high,uint method);
    [DllImport("kernel32.dll")] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr handle,uint wait);
    [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr process,uint exit);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
}}
