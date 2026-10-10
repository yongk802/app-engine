using System;
using System.IO;
using System.Collections;
using System.Collections.Generic;
using System.Web.Script.Serialization;

namespace AppEngineDesktop {
public static class Json {
    public static T Read<T>(string path) { return new JavaScriptSerializer().Deserialize<T>(File.ReadAllText(path)); }
    public static void Write<T>(string path,T value) {
        Directory.CreateDirectory(Path.GetDirectoryName(path));
        string temporary=path+"."+Guid.NewGuid().ToString("N")+".tmp";
        try {
            File.WriteAllText(temporary,new JavaScriptSerializer().Serialize(value));
            if(File.Exists(path)) File.Replace(temporary,path,null); else File.Move(temporary,path);
        } finally { if(File.Exists(temporary)) File.Delete(temporary); }
    }
}
public sealed class Settings {
    public string EngineRepository {get;set;}
    public string StoreRepository {get;set;}
    public string AppsDirectory {get;set;}
    public string StateDirectory {get;set;}
    public string StoreDataDirectory {get;set;}
    public int EnginePort {get;set;}
    public int StorePort {get;set;}
    public static Settings Defaults(string engine,string install) {
        string parent=Directory.GetParent(engine).FullName;
        return new Settings { EngineRepository=engine,StoreRepository=Path.Combine(parent,"app-store"),
            AppsDirectory=Path.Combine(parent,"personal-apps"),StateDirectory=Path.Combine(install,"app-state"),
            StoreDataDirectory=Path.Combine(parent,"app-store","store-data"),EnginePort=8770,StorePort=8780 };
    }
    public static Settings Load(string path) { return Json.Read<Settings>(path); }
    public static void Save(string path,Settings value) { Json.Write(path,value); }
    public void Validate() {
        if(EnginePort==StorePort) throw new Exception("Choose two different ports.");
        new Config("engine",this).Validate(); new Config("store",this).Validate();
    }
}
public sealed class Config {
    public readonly string Kind;
    public readonly Settings Settings;
    public Config(string kind,Settings settings) {
        if(kind!="engine" && kind!="store") throw new ArgumentException("Unknown service");
        Kind=kind; Settings=settings;
    }
    public string Title {get {return Kind=="engine" ? "App Engine" : "App Store";}}
    public string Repository {get {return Kind=="engine" ? Settings.EngineRepository : Settings.StoreRepository;}}
    public string Python {get {return Path.Combine(Repository,".venv","Scripts","python.exe");}}
    public int Port {get {return Kind=="engine" ? Settings.EnginePort : Settings.StorePort;}}
    public string Url {get {return "http://127.0.0.1:"+Port;}}
    public string ProbeUrl {get {return Url+(Kind=="engine" ? "/api/engine" : "/api/v1/catalog");}}
    public List<string> Arguments {get {
        return Kind=="engine" ? new List<string>{Path.Combine(Repository,"engine.py")} :
            new List<string>{"-m","app_store","serve","--data-dir",Settings.StoreDataDirectory,"--host","127.0.0.1","--port",Port.ToString(),"--name","My Apps"};
    }}
    public Dictionary<string,string> Environment {get {
        var env=new Dictionary<string,string>(StringComparer.OrdinalIgnoreCase);
        foreach(string key in new[]{"SystemRoot","WINDIR","TEMP","TMP","USERPROFILE","HOMEDRIVE","HOMEPATH","LOCALAPPDATA","APPDATA","ComSpec"}) {
            string value=System.Environment.GetEnvironmentVariable(key); if(value!=null) env[key]=value;
        }
        env["PATH"]=Path.GetDirectoryName(Python)+";"+(System.Environment.GetEnvironmentVariable("PATH") ?? Path.Combine(System.Environment.GetFolderPath(System.Environment.SpecialFolder.Windows),"System32"));
        env["HOME"]=System.Environment.GetFolderPath(System.Environment.SpecialFolder.UserProfile);
        env["PYTHONUNBUFFERED"]="1";
        if(Kind=="engine") {
            env["APP_ENGINE_APPS_DIR"]=Settings.AppsDirectory; env["APP_ENGINE_STATE_DIR"]=Settings.StateDirectory;
            env["APP_ENGINE_HOST"]="127.0.0.1"; env["APP_ENGINE_PORT"]=Port.ToString(); env["APP_ENGINE_PUBLIC_ORIGIN"]="";
        }
        return env;
    }}
    public static void ValidatePath(string path) {
        if(String.IsNullOrWhiteSpace(path) || !Path.IsPathRooted(path) || Path.GetPathRoot(path).Length<3 || path.IndexOfAny(Path.GetInvalidPathChars())>=0)
            throw new Exception("Use absolute folder paths in Settings.");
        if(File.Exists(path)) throw new Exception("A folder path points to a file: "+path);
    }
    public void Validate() {
        if(Port<1024 || Port>65535) throw new Exception("Ports must be between 1024 and 65535.");
        ValidatePath(Repository);
        string entry=Kind=="engine" ? "engine.py" : Path.Combine("app_store","__main__.py");
        if(!File.Exists(Path.Combine(Repository,entry))) throw new Exception("Choose the "+Title+" source folder in Settings.");
        if(!File.Exists(Python)) throw new Exception("Python environment missing: "+Python+". Run uv sync in this repository first.");
        if(Kind=="engine") {
            ValidatePath(Settings.AppsDirectory); ValidatePath(Settings.StateDirectory);
            if(!Directory.Exists(Settings.AppsDirectory)) throw new Exception("Choose an existing Apps folder in Settings.");
        } else ValidatePath(Settings.StoreDataDirectory);
    }
    public bool Recognizes(string json) {
        try {
            var value=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(json);
            object name; if(!value.TryGetValue("name",out name) || !(name is string)) return false;
            if(Kind=="engine") return (string)name=="app-engine";
            object protocol,apps;
            return value.TryGetValue("protocol_version",out protocol) && protocol is int && (int)protocol==1 &&
                value.TryGetValue("apps",out apps) && apps is IList;
        } catch { return false; }
    }
}}
