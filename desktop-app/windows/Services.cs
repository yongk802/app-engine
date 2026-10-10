using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Diagnostics;
using System.Management;
using System.Threading;
using System.Text;
using System.Security.Cryptography;

namespace AppEngineDesktop {
public sealed class Status {
    public bool Running,Managed;
    public string Message;
}
public sealed class Owner {
    public int ProcessId {get;set;}
    public long CreatedTicks {get;set;}
    public string Executable {get;set;}
    public string Token {get;set;}
}
public sealed class BrokerRequest {
    public Settings Settings {get;set;}
    public string Kind {get;set;}
    public string Runtime {get;set;}
    public string Token {get;set;}
}
public sealed class Manager {
    public readonly string Runtime,Executable;
    public Manager(string runtime,string executable) {Runtime=Path.GetFullPath(runtime); Executable=Path.GetFullPath(executable);}
    public string LogPath(string kind) {return Path.Combine(Runtime,kind+".log");}
    public string OwnerPath(string kind) {return Path.Combine(Runtime,kind+".owner.json");}
    public static string MutexName(string runtime,string kind,string purpose) {
        using(var hash=SHA256.Create()) {
            string id=BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(Path.GetFullPath(runtime).ToUpperInvariant()))).Replace("-","");
            return "Local\\AppEngineDesktop-"+id+"-"+kind+"-"+purpose;
        }
    }
    Process OwnedProcess(string kind) {
        Process process=null;
        try {
            Owner owner=Json.Read<Owner>(OwnerPath(kind));
            Guid token;
            if(!Guid.TryParseExact(owner.Token,"N",out token) || !String.Equals(owner.Executable,Executable,StringComparison.OrdinalIgnoreCase)) return null;
            process=Process.GetProcessById(owner.ProcessId);
            // Open and retain a handle before checking creation time. Stop acts on
            // this same object/handle, so a reused numeric PID cannot be targeted.
            IntPtr handle=process.Handle;
            if(process.HasExited || process.StartTime.ToUniversalTime().Ticks!=owner.CreatedTicks ||
               !String.Equals(process.MainModule.FileName,Executable,StringComparison.OrdinalIgnoreCase)) { process.Dispose(); return null; }
            using(var search=new ManagementObjectSearcher("SELECT CommandLine FROM Win32_Process WHERE ProcessId="+owner.ProcessId)) {
                string command=null;
                using(var results=search.Get()) {foreach(ManagementObject item in results) {using(item) command=item["CommandLine"] as string;}}
                if(command==null || !command.Contains(" --broker ") || !command.Contains(owner.Token)) {process.Dispose();return null;}
            }
            return process;
        } catch { if(process!=null) process.Dispose(); return null; }
    }
    public bool Owns(string kind) {using(var process=OwnedProcess(kind)) return process!=null;}
    public static bool PortAvailable(int port) {
        var listener=new TcpListener(IPAddress.Loopback,port);
        try {listener.Server.ExclusiveAddressUse=true; listener.Start();return true;}
        catch {return false;} finally {listener.Stop();}
    }
    bool Healthy(Config config) {
        try {
            var request=(HttpWebRequest)WebRequest.Create(config.ProbeUrl);
            request.Proxy=null; request.Timeout=1000; request.ReadWriteTimeout=1000;
            request.AllowAutoRedirect=false;
            using(var response=(HttpWebResponse)request.GetResponse()) {
                if(response.StatusCode!=HttpStatusCode.OK) return false;
                using(var reader=new StreamReader(response.GetResponseStream())) {
                    var text=new StringBuilder(); var buffer=new char[8192];int count;
                    while((count=reader.Read(buffer,0,buffer.Length))>0) {
                        if(text.Length+count>2*1024*1024) return false;
                        text.Append(buffer,0,count);
                    }
                    return config.Recognizes(text.ToString());
                }
            }
        } catch {return false;}
    }
    public Status Status(Config config) {
        bool managed=Owns(config.Kind),running=Healthy(config);
        return new Status {Running=running,Managed=managed,Message=running ?
            (managed ? "Running - port " : "Running externally - port ")+config.Port :
            managed ? "Not responding - check Logs or restart" :
            PortAvailable(config.Port) ? "Stopped" : "Port "+config.Port+" is occupied by another service"};
    }
    sealed class Gate : IDisposable {
        readonly Mutex mutex;
        public Gate(string name) {
            mutex=new Mutex(false,name);bool entered=false;
            try { entered=mutex.WaitOne(30000); } catch(AbandonedMutexException) {entered=true;}
            if(!entered) {mutex.Dispose();throw new Exception("Another launcher operation is still running.");}
        }
        public void Dispose() {mutex.ReleaseMutex();mutex.Dispose();}
    }
    public Status Start(Config config) {
        config.Validate();
        using(new Gate(MutexName(Runtime,config.Kind,"control"))) {
            Status current=Status(config); if(current.Running) return current;
            if(current.Managed) Stop(config.Kind);
            if(!PortAvailable(config.Port)) throw new Exception("Port "+config.Port+" is in use. Choose a different port in Settings.");
            Directory.CreateDirectory(Runtime);
            Directory.CreateDirectory(config.Kind=="engine" ? config.Settings.StateDirectory : config.Settings.StoreDataDirectory);
            string token=Guid.NewGuid().ToString("N"), requestPath=Path.Combine(Runtime,config.Kind+".request.json");
            Json.Write(requestPath,new BrokerRequest {Settings=config.Settings,Kind=config.Kind,Runtime=Runtime,Token=token});
            var start=new ProcessStartInfo(Executable,"--broker "+WindowsProcess.Quote(requestPath)+" "+token) {UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=Path.GetDirectoryName(Executable)};
            using(var broker=Process.Start(start)) {
                try {
                    DateTime deadline=DateTime.UtcNow.AddSeconds(20);
                    do {
                        if(broker.HasExited) throw new Exception(config.Title+" exited during startup. Open Logs for details.");
                        if(Owns(config.Kind) && Healthy(config)) return Status(config);
                        Thread.Sleep(200);
                    } while(DateTime.UtcNow<deadline);
                    throw new Exception(config.Title+" did not become ready. Open Logs for details.");
                } catch {
                    // This is the exact process we just created, even if it never
                    // published an owner record. Dispose alone does not stop it.
                    if(!broker.HasExited) {broker.Kill();broker.WaitForExit(20000);}
                    throw;
                }
            }
        }
    }
    public void Stop(string kind) {
        using(new Gate(MutexName(Runtime,kind,"control")))
        using(var process=OwnedProcess(kind)) {
            if(process==null) return;
            process.Kill();
            if(!process.WaitForExit(20000)) throw new Exception("Service is still stopping. Check its log.");
            // Closing the broker's job handle stops all descendants. Give the OS
            // a moment to close their sockets before status/restart is attempted.
            Thread.Sleep(200);
        }
    }
    public static int RunBroker(string requestPath,string token) {
        BrokerRequest request=Json.Read<BrokerRequest>(requestPath);
        if(request.Token!=token) throw new Exception("Invalid broker request.");
        Config config=new Config(request.Kind,request.Settings); config.Validate();
        var manager=new Manager(request.Runtime,Process.GetCurrentProcess().MainModule.FileName);
        using(new Gate(MutexName(manager.Runtime,config.Kind,"broker"))) {
            try {
                using(var child=new WindowsProcess(config,manager.LogPath(config.Kind))) {
                    using(var self=Process.GetCurrentProcess()) Json.Write(manager.OwnerPath(config.Kind),new Owner {
                        ProcessId=self.Id,CreatedTicks=self.StartTime.ToUniversalTime().Ticks,Executable=manager.Executable,Token=token});
                    child.Wait();
                }
                return 0;
            } catch(Exception error) {File.AppendAllText(manager.LogPath(config.Kind),"\r\nLauncher: "+error+"\r\n");return 1;}
        }
    }
}}
