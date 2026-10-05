// SPDX-License-Identifier: GPL-3.0-only
using System.Numerics;
using System.Text.Json;
using System.Security.Cryptography;
using System.Text.RegularExpressions;

namespace ArrayRuntime;

// Generic, closed, straight-line array interpreter. No file, image, network,
// reflection, scene selector or reconstruction pipeline exists in this module.
public static class Machine
{
    const int N=128, Count=N*N;
    static void Fail(string message)=>throw new InvalidDataException(message);
    static void Keys(JsonElement obj,params string[] expected)
    {
        if(obj.ValueKind!=JsonValueKind.Object)Fail("Expected object");
        var names=obj.EnumerateObject().Select(x=>x.Name).ToArray();
        if(names.Length!=expected.Length||!names.Order().SequenceEqual(expected.Order()))Fail("Unexpected or duplicate fields");
    }
    static string Text(JsonElement e)=>e.ValueKind==JsonValueKind.String?e.GetString()!:throw new InvalidDataException("Expected text");
    static double Number(JsonElement e,double max)
    {
        if(e.ValueKind!=JsonValueKind.Number||!e.TryGetDouble(out double n)||!double.IsFinite(n)||Math.Abs(n)>max)throw new InvalidDataException("Numeric bound");
        return n;
    }
    static int Integer(JsonElement e,int max)
    {
        if(e.ValueKind!=JsonValueKind.Number||!e.TryGetInt32(out int n)||Math.Abs((long)n)>max)throw new InvalidDataException("Integer bound");return n;
    }
    static byte[] Bytes(Complex[] values)
    {
        var data=new byte[Count*16];for(int i=0;i<Count;i++)
        {System.Buffers.Binary.BinaryPrimitives.WriteDoubleLittleEndian(data.AsSpan(i*16,8),values[i].Real);System.Buffers.Binary.BinaryPrimitives.WriteDoubleLittleEndian(data.AsSpan(i*16+8,8),values[i].Imaginary);}return data;
    }
    static void Fft(Complex[] data,bool inverse)
    {
        for(int i=1,j=0;i<N;i++){int bit=N>>1;for(;(j&bit)!=0;bit>>=1)j^=bit;j^=bit;if(i<j)(data[i],data[j])=(data[j],data[i]);}
        for(int len=2;len<=N;len<<=1)
        {
            double angle=(inverse?2:-2)*Math.PI/len;var root=new Complex(Math.Cos(angle),Math.Sin(angle));
            for(int i=0;i<N;i+=len){var w=Complex.One;for(int j=0;j<len/2;j++){var u=data[i+j];var v=data[i+j+len/2]*w;data[i+j]=u+v;data[i+j+len/2]=u-v;w*=root;}}
        }
        if(inverse)for(int i=0;i<N;i++)data[i]/=N;
    }
    static Complex[] Fft2(Complex[] source,bool inverse)
    {
        var result=(Complex[])source.Clone();var line=new Complex[N];
        for(int y=0;y<N;y++){Array.Copy(result,y*N,line,0,N);Fft(line,inverse);Array.Copy(line,0,result,y*N,N);}
        for(int x=0;x<N;x++){for(int y=0;y<N;y++)line[y]=result[y*N+x];Fft(line,inverse);for(int y=0;y<N;y++)result[y*N+x]=line[y];}return result;
    }
    public static string Execute(string programJson)
    {
        if(programJson.Length>56000)Fail("Program byte budget");
        using var document=JsonDocument.Parse(programJson,new JsonDocumentOptions{MaxDepth=12});var program=document.RootElement;
        Keys(program,"schema","input","nodes","output");if(Text(program.GetProperty("schema"))!="photon-pretzel/array-program/1")Fail("Program schema");
        var input=program.GetProperty("input");Keys(input,"encoding","shape","data");
        if(Text(input.GetProperty("encoding"))!="uint16-le/base64")Fail("Sensor encoding");var shape=input.GetProperty("shape");
        if(shape.ValueKind!=JsonValueKind.Array||shape.GetArrayLength()!=2||Integer(shape[0],N)!=N||Integer(shape[1],N)!=N)Fail("Sensor shape");
        string encoded=Text(input.GetProperty("data"));if(encoded.Length!=43692)Fail("Sensor size");byte[] sensor=Convert.FromBase64String(encoded);
        if(sensor.Length!=32768||Convert.ToBase64String(sensor)!=encoded)Fail("Noncanonical sensor encoding");
        var values=new Dictionary<string,Complex[]>(StringComparer.Ordinal);var initial=new Complex[Count];
        for(int i=0;i<Count;i++)initial[i]=System.Buffers.Binary.BinaryPrimitives.ReadUInt16LittleEndian(sensor.AsSpan(i*2,2));values["sensor"]=initial;
        var nodes=program.GetProperty("nodes");if(nodes.ValueKind!=JsonValueKind.Array||nodes.GetArrayLength()<1||nodes.GetArrayLength()>24)Fail("Instruction budget");
        var trace=new List<object>();
        foreach(var node in nodes.EnumerateArray())
        {
            string id=Text(node.GetProperty("id")),op=Text(node.GetProperty("op"));
            if(id.Length>24||!Regex.IsMatch(id,"\\A[a-z][a-z0-9_]*\\z")||values.ContainsKey(id))Fail("Invalid or duplicate identifier");
            Complex[] Ref(string key){string name=Text(node.GetProperty(key));if(!values.TryGetValue(name,out var found))throw new InvalidDataException("Forward or unknown reference");return found;}
            var output=new Complex[Count];
            switch(op)
            {
                case "scale":Keys(node,"id","op","src","factor");var src=Ref("src");double factor=Number(node.GetProperty("factor"),16);for(int i=0;i<Count;i++)output[i]=src[i]*factor;break;
                case "fft2":case "ifft2":Keys(node,"id","op","src");output=Fft2(Ref("src"),op=="ifft2");break;
                case "roll":
                    Keys(node,"id","op","src","shifts");var roll=Ref("src");var shifts=node.GetProperty("shifts");if(shifts.ValueKind!=JsonValueKind.Array||shifts.GetArrayLength()!=2)Fail("Roll shifts");
                    int dy=Integer(shifts[0],127),dx=Integer(shifts[1],127);for(int y=0;y<N;y++)for(int x=0;x<N;x++)output[((y+dy+N)%N)*N+(x+dx+N)%N]=roll[y*N+x];break;
                case "multiply":Keys(node,"id","op","a","b");var a=Ref("a");var b=Ref("b");for(int i=0;i<Count;i++)output[i]=a[i]*b[i];break;
                case "abs2":Keys(node,"id","op","src");var z=Ref("src");for(int i=0;i<Count;i++)output[i]=z[i].Magnitude*z[i].Magnitude;break;
                case "disk":
                    Keys(node,"id","op","radius");double radius=Number(node.GetProperty("radius"),32);if(radius<0)Fail("Disk radius");
                    for(int y=0;y<N;y++)for(int x=0;x<N;x++){int fx=x<64?x:x-N,fy=y<64?y:y-N;output[y*N+x]=fx*fx+fy*fy<=radius*radius?Complex.One:Complex.Zero;}break;
                case "transfer":
                    Keys(node,"id","op","pitch_m","wavelength_m","distance_m");double pitch=Number(node.GetProperty("pitch_m"),1),wavelength=Number(node.GetProperty("wavelength_m"),1),distance=Number(node.GetProperty("distance_m"),.02);
                    if(pitch!=8e-6||wavelength!=532e-9)Fail("Sampling envelope");
                    for(int y=0;y<N;y++)for(int x=0;x<N;x++)
                    {double fx=(x<64?x:x-N)/(N*pitch),fy=(y<64?y:y-N)/(N*pitch),square=Math.Pow(1/wavelength,2)-fx*fx-fy*fy;
                        output[y*N+x]=distance==0?Complex.One:square<0?Complex.Zero:Complex.FromPolarCoordinates(1,2*Math.PI*distance*Math.Sqrt(square));}break;
                default:Fail("Unsupported instruction");break;
            }
            foreach(var value in output)if(!double.IsFinite(value.Real)||!double.IsFinite(value.Imaginary)||value.Magnitude>1e12)Fail("Finite array result bound");
            values[id]=output;trace.Add(new {id,op,sha256=Convert.ToHexStringLower(SHA256.HashData(Bytes(output)))});
        }
        string selected=Text(program.GetProperty("output"));if(selected=="sensor"||!values.TryGetValue(selected,out var result))throw new InvalidDataException("Output must reference an instruction");
        byte[] bytes=Bytes(result);return JsonSerializer.Serialize(new {encoding="complex128-le/base64",shape=new[]{128,128},data=Convert.ToBase64String(bytes),sha256=Convert.ToHexStringLower(SHA256.HashData(bytes)),trace});
    }
}
