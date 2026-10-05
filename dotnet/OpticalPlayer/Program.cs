// SPDX-License-Identifier: GPL-3.0-only
using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
namespace OpticalPlayer;

internal static class Program
{
    internal static string Startup(string? path=null)
    {
        if(path is not null)return path;
        foreach(string name in new[]{"game.tiff","game.gif"}){string candidate=Path.Combine(AppContext.BaseDirectory,name);if(File.Exists(candidate))return candidate;}
        throw new FileNotFoundException("The demo image is missing. Extract the complete Photon Pretzel Windows package, then open OpticalPlayer.exe. The source ZIP is for developers.");
    }
    internal static JsonObject Gain(JsonObject payload,double gain)
    {
        if(!double.IsFinite(gain)||Math.Abs(gain)>16)throw new InvalidDataException("Gain must be finite and between -16 and 16");
        var result=(JsonObject)payload.DeepClone();var program=JsonNode.Parse(result["program_json"]!.GetValue<string>())!.AsObject();var nodes=program["nodes"]!.AsArray();
        if(nodes.Count>=24)throw new InvalidDataException("The image has reached its 24-instruction limit; reopen the original demo to start again.");
        string name=Enumerable.Range(0,24).Select(i=>"gain_"+i).First(n=>nodes.All(x=>x!["id"]!.GetValue<string>()!=n));
        nodes.Add(new JsonObject{["id"]=name,["op"]="scale",["src"]=program["output"]!.GetValue<string>(),["factor"]=gain});program["output"]=name;
        result["program_json"]=program.ToJsonString();return result;
    }
    internal static void Export(Bitmap image,string folder,JsonObject receipt)
    {
        if(Directory.Exists(folder)||File.Exists(folder))throw new IOException("Existing output is preserved; choose a new folder");
        Directory.CreateDirectory(folder);var original=Carrier.Canonical(Carrier.Decode(image));
        foreach(string extension in new[]{"tiff","gif"})
        {
            string path=Path.Combine(folder,"processing."+extension);Carrier.Save(image,path);using var reopened=Carrier.Open(path);
            var payload=Carrier.Decode(reopened);if(!Carrier.Canonical(payload).SequenceEqual(original))throw new InvalidDataException("Export changed its program or runtime");
            var repeated=Carrier.Execute(payload);if(repeated["sha256"]!.GetValue<string>()!=receipt["sha256"]!.GetValue<string>())throw new InvalidDataException("Reopened image computation changed");
        }
        using(var stream=new FileStream(Path.Combine(folder,"field.complex128-le"),FileMode.CreateNew))stream.Write(Carrier.OutputBytes(receipt));
        File.WriteAllText(Path.Combine(folder,"receipt.json"),Summary(receipt).ToJsonString(new JsonSerializerOptions{WriteIndented=true}));
    }
    internal static JsonObject Summary(JsonObject receipt)=>new(){["version"]="0.4.0",["result_sha256"]=receipt["sha256"]!.GetValue<string>(),["trace"]=receipt["trace"]!.DeepClone(),["runtime_sha256"]=TrustedRuntime.Sha256,["runtime_source"]="approved module recovered from image pixels",["computation_location"]="host CPU",["simulation_only"]=true};
    [STAThread] static int Main(string[] args)
    {
        bool cli=args.Any(x=>x.StartsWith("--",StringComparison.Ordinal));
        try
        {
            if(args.SequenceEqual(new[]{"--version"})){Console.WriteLine("0.4.0");return 0;}
            if(args.Length>0&&args[0]=="--execute")
            {
                if(args.Length<2)throw new ArgumentException("--execute IMAGE [--gain NUMBER] [--out NEW_FOLDER]");string? output=null;double? gain=null;
                for(int i=2;i<args.Length;i+=2)
                {
                    if(i+1>=args.Length)throw new ArgumentException("Missing option value");
                    if(args[i]=="--out"&&output is null)output=args[i+1];
                    else if(args[i]=="--gain"&&gain is null)gain=double.Parse(args[i+1],CultureInfo.InvariantCulture);
                    else throw new ArgumentException("Unknown or duplicate option");
                }
                using var input=Carrier.Open(args[1]);var payload=Carrier.Decode(input);Carrier.Execute(payload);
                if(gain is {} value)payload=Gain(payload,value);var receipt=Carrier.Execute(payload);using var fresh=Display.Render(payload,receipt,1);
                if(output is not null)Export(fresh,output,receipt);Console.WriteLine(Summary(receipt).ToJsonString());return 0;
            }
            bool smoke=args.Length>0&&args[0]=="--smoke-ui";string? source=null,export=null;
            if(smoke)
            {
                int position=1;if(position<args.Length&&!args[position].StartsWith("--"))source=args[position++];
                if(position<args.Length){if(position+2!=args.Length||args[position]!="--export-dir")throw new ArgumentException("--smoke-ui [IMAGE] [--export-dir NEW_FOLDER]");export=args[position+1];}
            }
            else{if(args.Length>1||cli)throw new ArgumentException("Open an image or use --execute / --smoke-ui");source=args.FirstOrDefault();}
            ApplicationConfiguration.Initialize();using var form=new Reader(Startup(source),smoke,export);Application.Run(form);return form.Failure?1:0;
        }
        catch(Exception error)
        {if(cli)Console.Error.WriteLine(error.Message);else MessageBox.Show(error.Message,"Photon Pretzel could not open",MessageBoxButtons.OK,MessageBoxIcon.Error);return 1;}
    }
}
internal sealed class Reader:Form
{
    readonly PictureBox picture=new(){Dock=DockStyle.Fill,SizeMode=PictureBoxSizeMode.Zoom,BackColor=Color.Black};
    readonly Label status=new(){Dock=DockStyle.Bottom,Height=56,Padding=new Padding(8)};
    readonly NumericUpDown gain=new(){Minimum=-16,Maximum=16,DecimalPlaces=2,Increment=.25m,Value=.5m,Width=70};
    readonly string source;readonly bool smoke;readonly string? export;int refresh;JsonObject? receipt;
    internal bool Failure{get;private set;}
    public Reader(string path,bool testing,string? exportFolder)
    {
        source=path;smoke=testing;export=exportFolder;Text="Photon Pretzel 0.4 — Compute from image pixels";ClientSize=new Size(820,900);MinimumSize=new Size(650,650);
        var controls=new FlowLayoutPanel{Dock=DockStyle.Top,Height=42,Padding=new Padding(6)};
        void Button(string text,Action action){var button=new Button{Text=text,AutoSize=true};button.Click+=(_,_)=>Guard(action);controls.Controls.Add(button);}
        Button("Run image",Run);controls.Controls.Add(gain);Button("Apply gain",Edit);Button("Open image",Open);Button("Save TIFF + GIF",Save);Button("Reset demo",()=>LoadImage(source));
        Controls.Add(picture);Controls.Add(status);Controls.Add(controls);
        Shown+=(_,_)=>Guard(()=>
        {
            LoadImage(source);
            if(smoke)
            {
                string first=receipt!["sha256"]!.GetValue<string>();var before=Carrier.Decode((Bitmap)picture.Image!);byte[] runtime=Carrier.Canonical(before["runtime"]);
                Edit();string edited=receipt!["sha256"]!.GetValue<string>();if(first==edited)throw new InvalidDataException("Gain edit did not change the output");
                Run();if(receipt!["sha256"]!.GetValue<string>()!=edited)throw new InvalidDataException("Refreshed image failed to reexecute");
                if(!Carrier.Canonical(Carrier.Decode((Bitmap)picture.Image!)["runtime"]).SequenceEqual(runtime))throw new InvalidDataException("Runtime changed during edit");
                if(export is not null)Program.Export((Bitmap)picture.Image!,export,receipt!);
                var report=Program.Summary(receipt!);report["ui_constructed"]=true;report["automatic_computation"]=true;report["pixel_refreshes"]=refresh;report["program_edit_changed_output"]=true;report["runtime_preserved"]=true;
                Console.WriteLine(report.ToJsonString());Close();
            }
        });
        FormClosed+=(_,_)=>picture.Image?.Dispose();
    }
    void Guard(Action action)
    {try{action();}catch(Exception error){Failure=true;status.Text="Cannot compute: "+error.Message;if(smoke){Console.Error.WriteLine(error.Message);Close();}else MessageBox.Show(this,error.Message,"Photon Pretzel",MessageBoxButtons.OK,MessageBoxIcon.Error);}}
    void Set(Bitmap next){var previous=picture.Image;picture.Image=next;previous?.Dispose();}
    void LoadImage(string path){using var image=Carrier.Open(path);var payload=Carrier.Decode(image);Refresh(payload);}
    void Refresh(JsonObject payload)
    {
        var watch=System.Diagnostics.Stopwatch.StartNew();var result=Carrier.Execute(payload);var image=Display.Render(payload,result,refresh+1);
        try{if(!Carrier.Canonical(Carrier.Decode(image)).SequenceEqual(Carrier.Canonical(payload)))throw new InvalidDataException("Refreshed carrier differs");}catch{image.Dispose();throw;}
        receipt=result;refresh++;Set(image);status.Text=$"Computed {result["trace"]!.AsArray().Count} operators from image pixels in {watch.ElapsedMilliseconds} ms.\nSave keeps the runtime, program and input together. The CPU executes this numerical simulation.";
    }
    void Run(){if(picture.Image is not Bitmap image)throw new InvalidDataException("Open an image first");Refresh(Carrier.Decode(image));}
    void Edit(){if(picture.Image is not Bitmap image)throw new InvalidDataException("Open an image first");Refresh(Program.Gain(Carrier.Decode(image),(double)gain.Value));}
    void Open(){using var dialog=new OpenFileDialog{Filter="Executable images|*.tiff;*.tif;*.gif",Title="Open a saved Photon image"};if(dialog.ShowDialog(this)==DialogResult.OK)LoadImage(dialog.FileName);}
    void Save()
    {
        if(picture.Image is not Bitmap image||receipt is null)throw new InvalidDataException("Compute an image first");
        using var dialog=new SaveFileDialog{Filter="New export folder name|*.export",FileName="Photon-result.export",Title="Choose a new folder name for the TIFF and GIF"};
        if(dialog.ShowDialog(this)==DialogResult.OK){Program.Export(image,dialog.FileName,receipt);status.Text="Saved TIFF and GIF. Both were reopened and computed again successfully.\n"+dialog.FileName;}
    }
}

