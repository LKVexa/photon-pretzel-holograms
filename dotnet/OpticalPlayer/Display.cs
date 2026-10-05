// SPDX-License-Identifier: GPL-3.0-only
using System.Buffers.Binary;
using System.Text.Json.Nodes;
namespace OpticalPlayer;
internal static class Display
{
    public static Bitmap Render(JsonObject payload,JsonObject receipt,int refresh)
    {
        var program=JsonNode.Parse(payload["program_json"]!.GetValue<string>())!.AsObject();
        byte[] samples=Convert.FromBase64String(program["input"]!["data"]!.GetValue<string>()),field=Carrier.OutputBytes(receipt);
        using var image=new Bitmap(Carrier.Width,Carrier.Height);using var g=Graphics.FromImage(image);g.Clear(Color.FromArgb(20,20,20));
        using var title=new Font("Segoe UI",19,FontStyle.Bold);using var text=new Font("Segoe UI",11);
        g.DrawString("PHOTON PRETZEL / RUNTIME IN PIXELS",title,Brushes.White,18,12);
        ushort max=0;for(int i=0;i<16384;i++)max=Math.Max(max,BinaryPrimitives.ReadUInt16LittleEndian(samples.AsSpan(i*2,2)));
        using var sensor=new Bitmap(128,128);using var output=new Bitmap(128,128);
        for(int y=0;y<128;y++)for(int x=0;x<128;x++)
        {
            int i=y*128+x;int s=max==0?0:(int)Math.Round(BinaryPrimitives.ReadUInt16LittleEndian(samples.AsSpan(i*2,2))*255.0/max);
            double re=BinaryPrimitives.ReadDoubleLittleEndian(field.AsSpan(i*16,8)),im=BinaryPrimitives.ReadDoubleLittleEndian(field.AsSpan(i*16+8,8));
            int intensity=(int)Math.Round(Math.Clamp(re*re+im*im,0,1)*255);sensor.SetPixel(x,y,Color.FromArgb(s,s,s));output.SetPixel(x,y,Color.FromArgb(intensity,intensity,intensity));
        }
        g.InterpolationMode=System.Drawing.Drawing2D.InterpolationMode.NearestNeighbor;g.PixelOffsetMode=System.Drawing.Drawing2D.PixelOffsetMode.Half;
        g.DrawImage(sensor,new Rectangle(48,58,256,256));g.DrawImage(output,new Rectangle(448,58,256,256));
        g.DrawString("Sensor samples stored in pixels",text,Brushes.White,32,320);g.DrawString("Output intensity: fixed 0 to 1",text,Brushes.White,432,320);
        g.DrawString($"{program["nodes"]!.AsArray().Count} image-carried operators | refreshed {refresh} | CPU execution",text,Brushes.White,24,344);
        g.DrawString("The runtime, operator graph and input samples are carried below.",text,Brushes.White,24,365);
        return Carrier.Encode(image,payload);
    }
}
