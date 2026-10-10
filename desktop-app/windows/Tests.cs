using System;
using System.IO;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;
namespace AppEngineDesktop {
public static class Tests {
    static int checks;
    static void Check(bool condition, string message) { checks++; if (!condition) throw new Exception(message); }
    static void Invalid(Action action) { bool failed=false; try { action(); } catch { failed=true; } Check(failed,"Expected invalid input"); }
    public static int Main(string[] args) {
        string root=Path.Combine(Path.GetTempPath(),"App Engine tests "+Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try {
            Settings settings=Settings.Defaults(Path.Combine(root,"app-engine"),Path.Combine(root,"install"));
            foreach(string repo in new[]{settings.EngineRepository,settings.StoreRepository}) {
                Directory.CreateDirectory(Path.Combine(repo,".venv","Scripts"));
                File.WriteAllText(Path.Combine(repo,".venv","Scripts","python.exe"),"");
            }
            File.WriteAllText(Path.Combine(settings.EngineRepository,"engine.py"),"");
            Directory.CreateDirectory(Path.Combine(settings.StoreRepository,"app_store"));
            File.WriteAllText(Path.Combine(settings.StoreRepository,"app_store","__main__.py"),"");
            Directory.CreateDirectory(settings.AppsDirectory);
            settings.Validate();
            Config engine=new Config("engine",settings); Config store=new Config("store",settings);
            Check(engine.Arguments.Count==1 && engine.Arguments[0]==Path.Combine(settings.EngineRepository,"engine.py"),"Literal path with spaces");
            Check(engine.Environment["APP_ENGINE_HOST"]=="127.0.0.1","Loopback host");
            Check(engine.Environment["APP_ENGINE_PUBLIC_ORIGIN"]=="","Local product mode");
            Check(store.Arguments.Contains(settings.StoreDataDirectory),"Literal catalog path");
            Check(engine.Recognizes("{\"name\":\"app-engine\"}"),"Engine identity");
            Check(!engine.Recognizes("{\"name\":\"atrium\"}"),"Reject foreign server");
            Check(store.Recognizes("{\"protocol_version\":1,\"name\":\"My Apps\",\"apps\":[]}"),"Store identity");
            Check(!store.Recognizes("{\"protocol_version\":true,\"name\":\"My Apps\",\"apps\":[]}"),"Reject boolean protocol");
            Check(WindowsProcess.Quote("C:\\path with spaces\\")=="\"C:\\path with spaces\\\\\"","Quote trailing slash");
            settings.StorePort=settings.EnginePort; Invalid(()=>settings.Validate()); settings.StorePort=8780;
            settings.EnginePort=0; Invalid(()=>settings.Validate()); settings.EnginePort=8770;
            File.Delete(engine.Python); Invalid(()=>settings.Validate());
            Settings.Save(Path.Combine(root,"settings.json"),settings);
            Check(Settings.Load(Path.Combine(root,"settings.json")).StoreRepository==settings.StoreRepository,"Settings round trip");
            Manager manager=new Manager(Path.Combine(root,"runtime"),args[0]);
            Check(!manager.Owns("engine"),"No implicit startup");
            manager.Stop("engine");
            if(args.Length==3) Integration(root,args[0],args[1],args[2]);
            Console.WriteLine("PASS: "+checks+" Windows launcher checks"); return 0;
        } catch(Exception error) { Console.Error.WriteLine(error); return 1; }
        finally { Directory.Delete(root,true); }
    }
    static void Integration(string root,string executable,string engineRepo,string storeRepo) {
        Settings settings=Settings.Defaults(engineRepo,Path.Combine(root,"real")); settings.StoreRepository=storeRepo;
        settings.AppsDirectory=Path.Combine(root,"empty apps"); Directory.CreateDirectory(settings.AppsDirectory);
        settings.StoreDataDirectory=Path.Combine(root,"catalog data"); settings.EnginePort=18770; settings.StorePort=18780;
        Manager manager=new Manager(Path.Combine(root,"real runtime"),executable);
        Manager outsider=new Manager(Path.Combine(root,"other runtime"),executable);
        try {
            foreach(string kind in new[]{"engine","store"}) {
                Config config=new Config(kind,settings);
                Check(Manager.PortAvailable(config.Port),"Test port occupied");
                Status started=manager.Start(config);
                Check(started.Running && started.Managed,"Real start: "+kind);
                Check(manager.Start(config).Running,"Idempotent start");
                Check(outsider.Status(config).Running && !outsider.Owns(kind),"External ownership");
                outsider.Stop(kind); Check(manager.Status(config).Running,"Foreign stop cannot stop service");
                Owner record=Json.Read<Owner>(manager.OwnerPath(kind));
                long ticks=record.CreatedTicks; record.CreatedTicks=ticks+1; Json.Write(manager.OwnerPath(kind),record);
                try {
                    Check(!manager.Owns(kind),"Reject stale/reused PID record"); manager.Stop(kind);
                    Check(outsider.Status(config).Running,"Stale record cannot stop live process");
                } finally {record.CreatedTicks=ticks; Json.Write(manager.OwnerPath(kind),record);}
                manager.Stop(kind); Check(!manager.Owns(kind) && !manager.Status(config).Running,"Stop and cleanup");
                Console.WriteLine("PASS: "+kind+" live start/health/reuse/external/stop");
            }
        } finally { manager.Stop("engine"); manager.Stop("store"); }
        DescendantCleanup(root,settings,engineRepo);
    }
    static void DescendantCleanup(string root,Settings real,string engineRepo) {
        string fake=Path.Combine(root,"child tree engine"), venv=Path.Combine(fake,".venv");
        Directory.CreateDirectory(Path.Combine(venv,"Scripts"));
        File.Copy(Path.Combine(engineRepo,".venv","Scripts","python.exe"),Path.Combine(venv,"Scripts","python.exe"));
        File.Copy(Path.Combine(engineRepo,".venv","pyvenv.cfg"),Path.Combine(venv,"pyvenv.cfg"));
        File.WriteAllText(Path.Combine(fake,"engine.py"),"import subprocess, sys, time\nchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\nprint(child.pid, flush=True)\ntime.sleep(300)\n");
        Settings settings=Settings.Defaults(fake,Path.Combine(root,"tree state"));
        settings.AppsDirectory=real.AppsDirectory;
        string log=Path.Combine(root,"tree.log"); int childId=0;
        using(var tree=new WindowsProcess(new Config("engine",settings),log)) {
            DateTime deadline=DateTime.UtcNow.AddSeconds(10);
            do {
                using(var reader=new StreamReader(new FileStream(log,FileMode.Open,FileAccess.Read,FileShare.ReadWrite))) {
                    Int32.TryParse(reader.ReadToEnd().Trim(),out childId);
                }
                if(childId!=0) break;
                Thread.Sleep(100);
            } while(DateTime.UtcNow<deadline);
            Check(childId!=0,"Child process startup");
            using(var child=Process.GetProcessById(childId)) {
                tree.Dispose();
                Check(child.WaitForExit(5000),"Job shutdown stops descendant");
            }
        }
        Console.WriteLine("PASS: service Job Object cleans up child process");
    }
}}
