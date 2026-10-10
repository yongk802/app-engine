using System;
using System.IO;
using System.Windows.Forms;
using System.Threading;
using System.Diagnostics;
using System.Runtime.InteropServices;
namespace AppEngineDesktop {
public static class Program {
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr window,int command);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
    [STAThread]
    public static int Main(string[] args) {
        if(args.Length==3 && args[0]=="--broker") {
            try {return Manager.RunBroker(args[1],args[2]);} catch {return 1;}
        }
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        try {
            string folder=Path.GetDirectoryName(Application.ExecutablePath);
            string path=args.Length==2 && args[0]=="--settings" ? Path.GetFullPath(args[1]) : Path.Combine(folder,"settings.json");
            bool created;
            using(var instance=new Mutex(true,Manager.MutexName(path,"ui","instance"),out created)) {
                if(!created) {
                    foreach(var process in Process.GetProcessesByName("AppEngine")) using(process) {
                        try {
                            if(process.Id!=Process.GetCurrentProcess().Id && process.SessionId==Process.GetCurrentProcess().SessionId &&
                               String.Equals(process.MainModule.FileName,Application.ExecutablePath,StringComparison.OrdinalIgnoreCase) && process.MainWindowHandle!=IntPtr.Zero) {
                                ShowWindow(process.MainWindowHandle,9); SetForegroundWindow(process.MainWindowHandle); break;
                            }
                        } catch { }
                    }
                    return 0;
                }
                try {Application.Run(new Launcher(path)); return 0;} finally {instance.ReleaseMutex();}
            }
        } catch(Exception error) {MessageBox.Show(error.Message,"App Engine",MessageBoxButtons.OK,MessageBoxIcon.Error);return 1;}
    }
}}
