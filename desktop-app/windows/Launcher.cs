using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace AppEngineDesktop {
public sealed class Launcher : Form {
    readonly string settingsPath;
    readonly Manager manager;
    readonly ServiceCard[] cards;
    readonly Button settingsButton;
    readonly System.Windows.Forms.Timer timer;
    Settings settings;
    bool operating, polling, closing;
    int revision;

    public Launcher(string settingsPath) {
        this.settingsPath=Path.GetFullPath(settingsPath);
        settings=Settings.Load(this.settingsPath);
        manager=new Manager(Path.Combine(Path.GetDirectoryName(this.settingsPath),"runtime"),Application.ExecutablePath);
        Text="App Engine";
        Icon=Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        Font=new Font("Segoe UI",9F);
        AutoScaleDimensions=new SizeF(7F,15F);
        AutoScaleMode=AutoScaleMode.Font;
        ClientSize=new Size(550,570);
        MinimumSize=new Size(566,609);
        StartPosition=FormStartPosition.CenterScreen;
        var layout=new TableLayoutPanel {Dock=DockStyle.Fill,Padding=new Padding(20),ColumnCount=1,RowCount=7};
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        for(int row=0;row<6;row++) layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent,100));
        layout.Controls.Add(new Label {Text="Your apps, one place.",Font=new Font(Font.FontFamily,22F,FontStyle.Bold),AutoSize=true,Margin=new Padding(0,0,0,6)},0,0);
        layout.Controls.Add(new Label {Text="Start a service, then open it in your browser.",AutoSize=true,Margin=new Padding(0,0,0,16)},0,1);
        cards=new[]{new ServiceCard("engine","App Engine","Run your apps and connect your chat."),
            new ServiceCard("store","App Store","Browse and share your app catalog.")};
        for(int index=0;index<cards.Length;index++) {
            ServiceCard card=cards[index];
            layout.Controls.Add(card,0,index+2);
            card.Start.Click+=delegate {Operate(card,"start");};
            card.Stop.Click+=delegate {Operate(card,"stop");};
            card.Open.Click+=delegate {Operate(card,"open");};
            card.Logs.Click+=delegate {OpenLogs(card);};
        }
        settingsButton=new Button {Text="Settings…",AutoSize=true,MinimumSize=new Size(100,30),AccessibleName="Launcher Settings",Margin=new Padding(0,0,0,10)};
        settingsButton.Click+=delegate {ShowSettings();};
        layout.Controls.Add(settingsButton,0,4);
        layout.Controls.Add(new Label {Text="Services start only when requested. Closing this window keeps running services available until you stop them or log out.",AutoSize=true,Dock=DockStyle.Fill,Margin=new Padding(0)},0,5);
        Controls.Add(layout);
        timer=new System.Windows.Forms.Timer {Interval=4000};
        timer.Tick+=delegate {RefreshStatus();};
        Shown+=delegate {RefreshStatus();timer.Start();};
        FormClosed+=delegate {closing=true;timer.Stop();timer.Dispose();};
        Render();
    }

    bool Available {get {return !closing && !IsDisposed;}}

    void Render() {
        if(!Available) return;
        foreach(ServiceCard card in cards) card.Update(operating);
        settingsButton.Enabled=!operating;
    }

    Status[] ReadStatuses(Settings snapshot) {
        var values=new Status[cards.Length];
        for(int index=0;index<cards.Length;index++) {
            try {values[index]=manager.Status(new Config(cards[index].Kind,snapshot));}
            catch(Exception error) {values[index]=new Status {Message="Could not check service: "+error.Message};}
        }
        return values;
    }

    async void RefreshStatus() {
        if(operating || polling || !Available) return;
        polling=true;
        int currentRevision=revision;
        Settings snapshot=settings;
        try {
            Status[] values=await Task.Run(()=>ReadStatuses(snapshot));
            if(Available && !operating && currentRevision==revision) {
                for(int index=0;index<cards.Length;index++) cards[index].Value=values[index];
                Render();
            }
        } finally {polling=false;}
    }

    async void Operate(ServiceCard card,string action) {
        if(operating || !Available) return;
        operating=true;
        revision++;
        Config config=new Config(card.Kind,settings);
        card.Value=new Status {Managed=card.Value.Managed,Message=action=="stop" ? "Stopping…" : action=="open" ? "Opening…" : "Starting…"};
        Render();
        Exception failure=null;
        try {
            await Task.Run(()=> {
                if(action=="stop") manager.Stop(card.Kind);
                else if(action=="start") manager.Start(config);
                else {
                    if(!manager.Status(config).Running) manager.Start(config);
                    Process.Start(new ProcessStartInfo(config.Url) {UseShellExecute=true});
                }
            });
        } catch(Exception error) {failure=error;}
        Status[] values=await Task.Run(()=>ReadStatuses(config.Settings));
        operating=false;
        if(!Available) return;
        for(int index=0;index<cards.Length;index++) cards[index].Value=values[index];
        Render();
        if(failure!=null) ShowError(failure.Message);
    }

    async void OpenLogs(ServiceCard card) {
        if(operating || !Available) return;
        string title=card.Text;
        try {
            await Task.Run(()=> {
                string path=manager.LogPath(card.Kind);
                if(!File.Exists(path)) throw new Exception("No log yet. Start "+title+" to create its log.");
                Process.Start(new ProcessStartInfo("notepad.exe",WindowsProcess.Quote(path)) {UseShellExecute=true});
            });
        } catch(Exception error) {if(Available) ShowError(error.Message);}
    }

    bool HasOwnedService() {return manager.Owns("engine") || manager.Owns("store");}

    async void ShowSettings() {
        if(operating || !Available) return;
        operating=true;
        revision++;
        Render();
        try {
            bool owned=await Task.Run(()=>HasOwnedService());
            if(!Available) return;
            if(owned) {
                ShowError("Stop both launcher-managed services before changing Settings.");
                return;
            }
            using(var editor=new SettingsDialog(settings,settingsPath,manager)) {
                if(editor.ShowDialog(this)==DialogResult.OK) settings=editor.SavedSettings;
            }
        } catch(Exception error) {if(Available) ShowError(error.Message);}
        finally {operating=false;if(Available) {Render();RefreshStatus();}}
    }

    void ShowError(string message) {MessageBox.Show(this,message,"App Engine",MessageBoxButtons.OK,MessageBoxIcon.Warning);}

    sealed class ServiceCard : GroupBox {
        public readonly string Kind;
        public readonly Button Start,Stop,Open,Logs;
        readonly Label status;
        public Status Value=new Status {Message="Checking…"};

        public ServiceCard(string kind,string title,string description) {
            Kind=kind;
            Text=title;
            Dock=DockStyle.Fill;
            AutoSize=true;
            Padding=new Padding(12,10,12,12);
            Margin=new Padding(0,0,0,14);
            var layout=new TableLayoutPanel {Dock=DockStyle.Top,AutoSize=true,ColumnCount=1,RowCount=3};
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
            for(int row=0;row<3;row++) layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.Controls.Add(new Label {Text=description,AutoSize=true,Dock=DockStyle.Fill,Margin=new Padding(0,4,0,8)},0,0);
            status=new Label {Text=Value.Message,AutoSize=true,Dock=DockStyle.Fill,MinimumSize=new Size(0,32),AccessibleName=title+" status",Margin=new Padding(0,0,0,4)};
            layout.Controls.Add(status,0,1);
            var buttons=new FlowLayoutPanel {AutoSize=true,Dock=DockStyle.Fill,WrapContents=true,Margin=new Padding(0)};
            Start=MakeButton("Start",title);
            Stop=MakeButton("Stop",title);
            Open=MakeButton("Open",title);
            Logs=MakeButton("Logs",title);
            buttons.Controls.AddRange(new Control[]{Start,Stop,Open,Logs});
            layout.Controls.Add(buttons,0,2);
            Controls.Add(layout);
        }

        static Button MakeButton(string action,string title) {
            return new Button {Text=action,AccessibleName=action+" "+title,AutoSize=true,MinimumSize=new Size(80,30),Margin=new Padding(0,0,8,0)};
        }

        public void Update(bool busy) {
            status.Text=Value.Message;
            Start.Enabled=!busy && !Value.Running;
            Stop.Enabled=!busy && Value.Managed;
            Open.Enabled=!busy;
            Logs.Enabled=!busy;
        }
    }

    sealed class SettingsDialog : Form {
        readonly string settingsPath;
        readonly Manager manager;
        readonly TextBox[] fields;
        readonly Button save,cancel;
        bool saving;
        public Settings SavedSettings;

        public SettingsDialog(Settings current,string settingsPath,Manager manager) {
            this.settingsPath=settingsPath;
            this.manager=manager;
            Text="Launcher Settings";
            Font=new Font("Segoe UI",9F);
            AutoScaleDimensions=new SizeF(7F,15F);
            AutoScaleMode=AutoScaleMode.Font;
            ClientSize=new Size(700,365);
            MinimumSize=new Size(716,404);
            StartPosition=FormStartPosition.CenterParent;
            ShowInTaskbar=false;
            MaximizeBox=false;
            MinimizeBox=false;
            var layout=new TableLayoutPanel {Dock=DockStyle.Fill,Padding=new Padding(20),ColumnCount=2,RowCount=9};
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
            for(int row=0;row<9;row++) layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            var explanation=new Label {Text="Choose local source folders with existing uv Python environments. Servers listen on this PC only.",AutoSize=true,Dock=DockStyle.Fill,Margin=new Padding(0,0,0,16)};
            layout.Controls.Add(explanation,0,0);
            layout.SetColumnSpan(explanation,2);
            string[] labels={"App Engine folder","App Store folder","Apps folder","App state folder","Store data folder","App Engine port","App Store port"};
            string[] values={current.EngineRepository,current.StoreRepository,current.AppsDirectory,current.StateDirectory,current.StoreDataDirectory,current.EnginePort.ToString(),current.StorePort.ToString()};
            fields=new TextBox[labels.Length];
            for(int index=0;index<labels.Length;index++) {
                layout.Controls.Add(new Label {Text=labels[index],AutoSize=true,Anchor=AnchorStyles.Left,Margin=new Padding(0,4,16,8)},0,index+1);
                fields[index]=new TextBox {Text=values[index],Dock=DockStyle.Fill,AccessibleName=labels[index],Margin=new Padding(0,0,0,8)};
                layout.Controls.Add(fields[index],1,index+1);
            }
            var buttons=new FlowLayoutPanel {Dock=DockStyle.Fill,AutoSize=true,FlowDirection=FlowDirection.RightToLeft,Margin=new Padding(0,8,0,0)};
            save=new Button {Text="Save",AccessibleName="Save Launcher Settings",MinimumSize=new Size(85,30),AutoSize=true};
            cancel=new Button {Text="Cancel",AccessibleName="Cancel Launcher Settings",MinimumSize=new Size(85,30),AutoSize=true,DialogResult=DialogResult.Cancel};
            buttons.Controls.Add(save);
            buttons.Controls.Add(cancel);
            layout.Controls.Add(buttons,0,8);
            layout.SetColumnSpan(buttons,2);
            Controls.Add(layout);
            AcceptButton=save;
            CancelButton=cancel;
            save.Click+=delegate {SaveSettings();};
            FormClosing+=delegate(object sender,FormClosingEventArgs args) {if(saving) args.Cancel=true;};
        }

        async void SaveSettings() {
            if(saving) return;
            int enginePort,storePort;
            if(!Int32.TryParse(fields[5].Text.Trim(),out enginePort) || !Int32.TryParse(fields[6].Text.Trim(),out storePort)) {
                MessageBox.Show(this,"Enter a whole number for each port.",Text,MessageBoxButtons.OK,MessageBoxIcon.Warning);
                return;
            }
            var candidate=new Settings {EngineRepository=fields[0].Text.Trim(),StoreRepository=fields[1].Text.Trim(),
                AppsDirectory=fields[2].Text.Trim(),StateDirectory=fields[3].Text.Trim(),StoreDataDirectory=fields[4].Text.Trim(),
                EnginePort=enginePort,StorePort=storePort};
            saving=true;
            save.Enabled=false;
            cancel.Enabled=false;
            foreach(TextBox field in fields) field.Enabled=false;
            try {
                await Task.Run(()=> {
                    candidate.Validate();
                    if(manager.Owns("engine") || manager.Owns("store")) throw new Exception("Stop both launcher-managed services before changing Settings.");
                    Settings.Save(settingsPath,candidate);
                });
                SavedSettings=candidate;
                saving=false;
                DialogResult=DialogResult.OK;
                Close();
            } catch(Exception error) {
                saving=false;
                save.Enabled=true;
                cancel.Enabled=true;
                foreach(TextBox field in fields) field.Enabled=true;
                MessageBox.Show(this,error.Message,Text,MessageBoxButtons.OK,MessageBoxIcon.Warning);
            }
        }
    }
}
}
