// SPDX-License-Identifier: GPL-3.0-only
using System.Drawing.Imaging;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
namespace OpticalPlayer;
internal static class Carrier
{
    public const int Width=768,Height=768,Y0=384,MaxPayload=72000;
    public static string Hash(byte[] b)=>Convert.ToHexStringLower(SHA256.HashData(b));
    sealed class UnicodeScalarComparer : IComparer<string>
    {
        // Python sorts Unicode code points, not UTF-16 code units.
        public int Compare(string? left,string? right)
        {
            using var a=(left??"").EnumerateRunes().GetEnumerator();
            using var b=(right??"").EnumerateRunes().GetEnumerator();
            while(true)
            {
                bool moreA=a.MoveNext(),moreB=b.MoveNext();
                if(!moreA||!moreB)return moreA.CompareTo(moreB);
                int order=a.Current.Value.CompareTo(b.Current.Value);
                if(order!=0)return order;
            }
        }
    }
    public static byte[] Canonical(JsonNode? node)
    {
        string Walk(JsonNode? n)
        {
            if(n is null)return "null";
            if(n is JsonObject o)return "{"+string.Join(",",o.OrderBy(x=>x.Key,new UnicodeScalarComparer()).Select(x=>Quote(x.Key)+":"+Walk(x.Value)))+"}";
            if(n is JsonArray a)return "["+string.Join(",",a.Select(Walk))+"]";
            var v=(JsonValue)n;
            if(v.TryGetValue<string>(out var s))return Quote(s);
            // Serialize primitive-backed and parsed JsonValues through the same
            // integer domain; normalize -0 and reject decimal/exponent tokens.
            var element=JsonSerializer.SerializeToElement(v);
            if(element.ValueKind==JsonValueKind.True)return "true";
            if(element.ValueKind==JsonValueKind.False)return "false";
            if(element.ValueKind==JsonValueKind.Number&&element.TryGetInt64(out long integer))
                return integer.ToString(System.Globalization.CultureInfo.InvariantCulture);
            throw new InvalidDataException("Payload values must use integer JSON numbers");
        }
        return Encoding.ASCII.GetBytes(Walk(node));
    }
    static string Quote(string s)
    {
        var b=new StringBuilder("\"");
        foreach(char c in s)
        {
            b.Append(c switch { '"'=>"\\\"", '\\'=>"\\\\", '\b'=>"\\b", '\f'=>"\\f", '\n'=>"\\n", '\r'=>"\\r", '\t'=>"\\t",
                _ => c<32||c>=127 ? "\\u"+((int)c).ToString("x4") : c.ToString() });
        }
        return b.Append('"').ToString();
    }

    static void Keys(JsonObject obj,params string[] fields)
    {if(!obj.Select(x=>x.Key).Order().SequenceEqual(fields.Order()))throw new InvalidDataException("Unexpected envelope fields");}
    public static void Validate(JsonObject payload)
    {
        Keys(payload,"schema","program_json","runtime");
        if(payload["schema"]?.GetValue<string>()!="photon-pretzel/executable-image/2")throw new InvalidDataException("Use a v0.4 runtime-bearing image");
        string program=payload["program_json"]?.GetValue<string>()??throw new InvalidDataException("Missing program");
        if(program.Length>56000)throw new InvalidDataException("Program length bound");
        var runtime=payload["runtime"] as JsonObject??throw new InvalidDataException("Missing runtime");Keys(runtime,"format","sha256","data");
        if(runtime["format"]?.GetValue<string>()!="dotnet-il/gzip-base64"||runtime["sha256"]?.GetValue<string>()!=TrustedRuntime.Sha256)throw new InvalidDataException("Runtime is not approved by this player build");
        string data=runtime["data"]?.GetValue<string>()??throw new InvalidDataException("Missing module");if(data.Length>24000)throw new InvalidDataException("Compressed runtime byte budget");
    }
    static byte[] Pixels(Bitmap image)
    {
        var locked=image.LockBits(new Rectangle(0,0,Width,Height),ImageLockMode.ReadOnly,PixelFormat.Format32bppArgb);
        try{if(locked.Stride!=Width*4)throw new InvalidDataException("Image stride");var data=new byte[Width*Height*4];Marshal.Copy(locked.Scan0,data,0,data.Length);return data;}
        finally{image.UnlockBits(locked);}
    }
    public static JsonObject Decode(Bitmap image)
    {
        if(image.Width!=Width||image.Height!=Height)throw new InvalidDataException("Image dimensions");var pixels=Pixels(image);
        byte[] Read(int count)
        {
            var result=new byte[count];for(int i=0;i<count;i++)
            {int x=(i%384)*2,y=Y0+(i/384)*2;byte value=pixels[(y*Width+x)*4];
                for(int yy=0;yy<2;yy++)for(int xx=0;xx<2;xx++)
                {int p=((y+yy)*Width+x+xx)*4;if(pixels[p]!=value||pixels[p+1]!=value||pixels[p+2]!=value||pixels[p+3]!=255)throw new InvalidDataException("Damaged or resampled executable cells");}result[i]=value;}return result;
        }
        byte[] header=Read(44);if(Encoding.ASCII.GetString(header,0,8)!="PPHVM004")throw new InvalidDataException("Use a v0.4 TIFF or GIF containing its runtime");
        uint length=System.Buffers.Binary.BinaryPrimitives.ReadUInt32BigEndian(header.AsSpan(8,4));if(length<1||length>MaxPayload)throw new InvalidDataException("Payload byte budget");
        byte[] raw=Read(44+(int)length).AsSpan(44).ToArray();if(!CryptographicOperations.FixedTimeEquals(SHA256.HashData(raw),header.AsSpan(12,32)))throw new InvalidDataException("Pixel checksum mismatch");
        var payload=JsonNode.Parse(raw,documentOptions:new JsonDocumentOptions{MaxDepth=8}) as JsonObject??throw new InvalidDataException("Envelope object required");
        if(!Canonical(payload).SequenceEqual(raw))throw new InvalidDataException("Duplicate or noncanonical envelope");Validate(payload);return payload;
    }
    public static Bitmap Open(string path)
    {
        byte[] snapshot=ImagePreflight.Snapshot(path);var info=ImagePreflight.Check(snapshot);
        if(info.Format is not "TIFF" and not "GIF")throw new InvalidDataException("Use TIFF or GIF");
        using var stream=new MemoryStream(snapshot,false);using var source=Image.FromStream(stream,false,true);
        var dimension=source.FrameDimensionsList.Contains(FrameDimension.Page.Guid)?FrameDimension.Page:FrameDimension.Time;
        int count=source.GetFrameCount(dimension);if(count!=info.Frames)throw new InvalidDataException("Decoder frame count mismatch");
        Bitmap? result=null;byte[]? previous=null;
        try
        {
            for(int i=0;i<count;i++)
            {
                source.SelectActiveFrame(dimension,i);if(source.Width!=Width||source.Height!=Height)throw new InvalidDataException("Frame dimensions");
                var frame=new Bitmap(Width,Height,PixelFormat.Format32bppArgb);using(var g=Graphics.FromImage(frame))g.DrawImageUnscaled(source,0,0);
                byte[] current;try{current=Canonical(Decode(frame));}catch{frame.Dispose();throw;}
                if(previous is not null&&!previous.SequenceEqual(current)){frame.Dispose();throw new InvalidDataException("Frames contain different programs or runtimes");}
                result?.Dispose();result=frame;previous=current;
            }
            return result??throw new InvalidDataException("Empty carrier");
        }
        catch{result?.Dispose();throw;}
    }
    public static Bitmap Encode(Bitmap visible,JsonObject payload)
    {
        Validate(payload);byte[] raw=Canonical(payload);if(raw.Length>MaxPayload)throw new InvalidDataException("Envelope byte budget");
        var packet=new byte[44+raw.Length];Encoding.ASCII.GetBytes("PPHVM004").CopyTo(packet,0);System.Buffers.Binary.BinaryPrimitives.WriteInt32BigEndian(packet.AsSpan(8,4),raw.Length);SHA256.HashData(raw).CopyTo(packet,12);raw.CopyTo(packet,44);
        var output=new Bitmap(Width,Height,PixelFormat.Format32bppArgb);using(var g=Graphics.FromImage(output)){g.Clear(Color.White);g.DrawImageUnscaled(visible,0,0);g.FillRectangle(Brushes.White,0,Y0,Width,Height-Y0);}
        var locked=output.LockBits(new Rectangle(0,0,Width,Height),ImageLockMode.ReadWrite,PixelFormat.Format32bppArgb);
        try
        {
            var data=new byte[Width*Height*4];Marshal.Copy(locked.Scan0,data,0,data.Length);
            for(int i=0;i<packet.Length;i++){int x=(i%384)*2,y=Y0+(i/384)*2;for(int yy=0;yy<2;yy++)for(int xx=0;xx<2;xx++){int p=((y+yy)*Width+x+xx)*4;data[p]=data[p+1]=data[p+2]=packet[i];data[p+3]=255;}}
            Marshal.Copy(data,0,locked.Scan0,data.Length);
        }
        finally{output.UnlockBits(locked);}return output;
    }
    static MethodInfo? execute;
    public static JsonObject Execute(JsonObject payload)
    {
        Validate(payload);var r=payload["runtime"]!.AsObject();string encoded=r["data"]!.GetValue<string>();var compressed=Convert.FromBase64String(encoded);
        if(Convert.ToBase64String(compressed)!=encoded)throw new InvalidDataException("Runtime base64 is noncanonical");
        using var zip=new GZipStream(new MemoryStream(compressed),CompressionMode.Decompress);using var result=new MemoryStream();byte[] buffer=new byte[4096];int n;
        while((n=zip.Read(buffer))>0){if(result.Length+n>131072)throw new InvalidDataException("Runtime expansion budget");result.Write(buffer,0,n);}byte[] module=result.ToArray();
        if(Hash(module)!=TrustedRuntime.Sha256)throw new InvalidDataException("Runtime module checksum differs");
        // The SHA allowlist approves code; the image checksum alone is not a signature.
        execute??=Assembly.Load(module).GetType("ArrayRuntime.Machine",true)!.GetMethod("Execute",BindingFlags.Public|BindingFlags.Static)??throw new InvalidDataException("Runtime entry point");
        string raw;try{raw=(string)execute.Invoke(null,new object[]{payload["program_json"]!.GetValue<string>()})!;}catch(TargetInvocationException e){throw new InvalidDataException(e.InnerException?.Message??"Runtime failed",e.InnerException);}
        // The JSON encoder may escape each base64 character as six ASCII bytes.
        if(raw.Length>2200000)throw new InvalidDataException("Runtime output byte budget");return JsonNode.Parse(raw)!.AsObject();
    }
    public static byte[] OutputBytes(JsonObject receipt)
    {
        var raw=Convert.FromBase64String(receipt["data"]!.GetValue<string>());if(raw.Length!=128*128*16||Hash(raw)!=receipt["sha256"]!.GetValue<string>())throw new InvalidDataException("Runtime output shape or checksum");return raw;
    }
    public static void Save(Bitmap bitmap,string path)
    {
        string ext=Path.GetExtension(path).ToLowerInvariant();if(ext is not ".tiff" and not ".tif" and not ".gif")throw new InvalidDataException("Export must be TIFF or GIF");
        using var file=new FileStream(path,FileMode.CreateNew,FileAccess.Write,FileShare.None);if(ext!=".gif"){bitmap.Save(file,ImageFormat.Tiff);return;}
        using var indexed=new Bitmap(Width,Height,PixelFormat.Format8bppIndexed);var palette=indexed.Palette;for(int i=0;i<256;i++)palette.Entries[i]=Color.FromArgb(i,i,i);indexed.Palette=palette;
        var pixels=Pixels(bitmap);var locked=indexed.LockBits(new Rectangle(0,0,Width,Height),ImageLockMode.WriteOnly,PixelFormat.Format8bppIndexed);
        try{var data=new byte[locked.Stride*Height];for(int y=0;y<Height;y++)for(int x=0;x<Width;x++){int p=(y*Width+x)*4;data[y*locked.Stride+x]=(byte)((pixels[p]+pixels[p+1]+pixels[p+2])/3);}Marshal.Copy(data,0,locked.Scan0,data.Length);}finally{indexed.UnlockBits(locked);}indexed.Save(file,ImageFormat.Gif);
    }
}
