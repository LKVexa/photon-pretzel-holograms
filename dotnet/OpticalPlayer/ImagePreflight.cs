// SPDX-License-Identifier: GPL-3.0-only
using System.Buffers.Binary;
using System.Text;

namespace OpticalPlayer;

// A bounded structural screen before GDI+. This is deliberately a narrow image
// profile, not a substitute implementation of the image decompression codecs.
internal static class ImagePreflight
{
    const int MaxBytes=32*1024*1024,MaxFrames=32;
    internal readonly record struct Info(string Format,int Frames);
    static void Fail(string message)=>throw new InvalidDataException("Image preflight: "+message);
    static void Range(byte[] data,long offset,long count)
    {
        if(offset<0||count<0||offset>data.Length||count>data.Length-offset)Fail("truncated or invalid offset");
    }
    public static byte[] Snapshot(string path)
    {
        if((File.GetAttributes(path)&(FileAttributes.Directory|FileAttributes.ReparsePoint))!=0)
            Fail("input must be a regular file, not a directory or link");
        using var stream=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read);
        long length=stream.Length;
        if(length<8||length>MaxBytes)Fail("file must contain 8 bytes to 32 MiB");
        var bytes=new byte[(int)length];stream.ReadExactly(bytes);
        if(stream.ReadByte()!=-1)Fail("file changed while taking snapshot");
        return bytes;
    }
    public static Info Check(byte[] data)
    {
        if(data.Length<8||data.Length>MaxBytes)Fail("file size limit");
        if(data.AsSpan(0,6).SequenceEqual("GIF87a"u8)||data.AsSpan(0,6).SequenceEqual("GIF89a"u8))return Gif(data);
        if(data.AsSpan(0,8).SequenceEqual(new byte[]{137,80,78,71,13,10,26,10}))return Png(data);
        if((data[0]=='I'&&data[1]=='I')||(data[0]=='M'&&data[1]=='M'))return Tiff(data);
        Fail("use supported classic TIFF, GIF or PNG");return default;
    }
    static readonly HashSet<ushort> TiffTags=new(){
        254,255,256,257,258,259,262,266,269,270,271,272,273,274,277,278,279,
        282,283,284,285,286,287,296,297,305,306,315,316,317,320,338,339};
    static Info Tiff(byte[] data)
    {
        bool little=data[0]=='I';
        ushort U16(long offset){Range(data,offset,2);return little?BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan((int)offset,2)):BinaryPrimitives.ReadUInt16BigEndian(data.AsSpan((int)offset,2));}
        uint U32(long offset){Range(data,offset,4);return little?BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan((int)offset,4)):BinaryPrimitives.ReadUInt32BigEndian(data.AsSpan((int)offset,4));}
        if(U16(2)!=42)Fail("only classic TIFF is supported; BigTIFF is excluded");
        long next=U32(4);var visited=new HashSet<long>();int frames=0;long metadataBytes=0;
        while(next!=0)
        {
            if(++frames>MaxFrames||!visited.Add(next))Fail("TIFF frame limit or cyclic page chain");
            if(next<8)Fail("TIFF page offset overlaps header");
            int count=U16(next);if(count<1||count>128)Fail("TIFF tag count limit");
            Range(data,next+2,12L*count+4);
            var values=new Dictionary<ushort,(ushort Type,uint Count,long Offset)>();
            for(int i=0;i<count;i++)
            {
                long at=next+2+12L*i;ushort tag=U16(at),type=U16(at+2);uint items=U32(at+4);
                if(!TiffTags.Contains(tag)||values.ContainsKey(tag))Fail("unsupported or duplicate TIFF tag");
                int size=type switch{1 or 2 or 6 or 7=>1,3 or 8=>2,4 or 9 or 11=>4,5 or 10 or 12=>8,_=>0};
                if(size==0||items==0)Fail("unsupported TIFF field type or count");
                long bytes=(long)size*items,offset=bytes<=4?at+8:U32(at+8);
                metadataBytes+=bytes;
                if(bytes>65536||metadataBytes>2*1024*1024)Fail("TIFF metadata byte budget exceeded");
                Range(data,offset,bytes);values.Add(tag,(type,items,offset));
            }
            uint[] Numbers(ushort tag,bool required=false,uint fallback=0)
            {
                if(!values.TryGetValue(tag,out var entry))
                {if(required)Fail("missing required TIFF tag");return new[]{fallback};}
                if(entry.Type is not 3 and not 4||entry.Count>Carrier.Height)Fail("TIFF numeric type/count limit");
                var result=new uint[entry.Count];
                for(int i=0;i<result.Length;i++)result[i]=entry.Type==3?U16(entry.Offset+2L*i):U32(entry.Offset+4L*i);
                return result;
            }
            uint Scalar(ushort tag,bool required=false,uint fallback=0)
            {var numbers=Numbers(tag,required,fallback);if(numbers.Length!=1)Fail("TIFF scalar field has multiple values");return numbers[0];}
            if(Scalar(256,true)!=Carrier.Width||Scalar(257,true)!=Carrier.Height)Fail("TIFF frame dimensions differ");
            uint compression=Scalar(259,false,1);
            if(compression is not 1 and not 5 and not 8 and not 32773 and not 32946)Fail("unsupported TIFF compression");
            if(Scalar(284,false,1)!=1||Scalar(274,false,1)!=1||Scalar(266,false,1)!=1)Fail("unsupported TIFF planar/orientation/bit order");
            uint channels=Scalar(277,false,1),photo=Scalar(262,true);
            if(channels<1||channels>4||photo>3)Fail("unsupported TIFF sample/photometric layout");
            var bits=Numbers(258,false,1);
            if(bits.Length!=channels||bits.Any(b=>b is not 1 and not 2 and not 4 and not 8 and not 16))Fail("unsupported TIFF bit depth");
            if(values.ContainsKey(339)&&Numbers(339).Any(n=>n!=1))Fail("only unsigned TIFF samples are supported");
            var offsets=Numbers(273,true);var lengths=Numbers(279,true);
            uint rows=Scalar(278,false,Carrier.Height);
            if(rows==0||offsets.Length!=lengths.Length||offsets.Length!=(Carrier.Height+(long)rows-1)/rows)Fail("invalid TIFF strip layout");
            long compressedTotal=0;
            for(int i=0;i<offsets.Length;i++)
            {
                if(lengths[i]==0)Fail("empty TIFF strip");Range(data,offsets[i],lengths[i]);
                compressedTotal+=lengths[i];if(compressedTotal>MaxBytes)Fail("TIFF compressed sample budget exceeded");
            }
            next=U32(next+2+12L*count);
        }
        if(frames==0)Fail("TIFF has no pages");return new("TIFF",frames);
    }
    static Info Gif(byte[] data)
    {
        Range(data,0,13);int U16(int p)=>BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(p,2));
        if(U16(6)!=Carrier.Width||U16(8)!=Carrier.Height)Fail("GIF logical dimensions differ");
        int position=13,frames=0,blocks=0;bool global=(data[10]&128)!=0;
        if(global){int bytes=3*(2<<(data[10]&7));Range(data,position,bytes);position+=bytes;}
        void SubBlocks()
        {
            while(true)
            {
                if(++blocks>200000)Fail("GIF sub-block budget exceeded");
                Range(data,position,1);int length=data[position++];if(length==0)return;
                Range(data,position,length);position+=length;
            }
        }
        while(true)
        {
            Range(data,position,1);int marker=data[position++];
            if(marker==0x3b)
            {if(frames<1||position!=data.Length)Fail("empty GIF or trailing bytes");return new("GIF",frames);}
            if(marker==0x21)
            {
                Range(data,position,2);int label=data[position++];
                if(label==0xf9)
                {Range(data,position,6);if(data[position]!=4||data[position+5]!=0)Fail("invalid GIF control extension");position+=6;}
                else if(label==0xff)
                {if(data[position]!=11)Fail("invalid GIF application extension");SubBlocks();}
                else if(label==0xfe)SubBlocks();
                else Fail("unsupported GIF extension");
                if(++blocks>200000)Fail("GIF block budget exceeded");
                continue;
            }
            if(marker!=0x2c)Fail("invalid GIF block marker");
            if(++frames>MaxFrames)Fail("GIF frame limit exceeded");
            Range(data,position,9);
            int left=U16(position),top=U16(position+2),width=U16(position+4),height=U16(position+6),packed=data[position+8];
            if(width<1||height<1||left+width>Carrier.Width||top+height>Carrier.Height)Fail("GIF frame rectangle exceeds canvas");
            position+=9;
            if((packed&128)!=0){int bytes=3*(2<<(packed&7));Range(data,position,bytes);position+=bytes;}
            else if(!global)Fail("GIF image has no palette");
            Range(data,position,1);int codeSize=data[position++];if(codeSize<2||codeSize>8)Fail("invalid GIF LZW code size");
            SubBlocks();
        }
    }
    static readonly uint[] CrcTable=MakeCrcTable();
    static uint[] MakeCrcTable()
    {
        var table=new uint[256];for(uint n=0;n<256;n++){uint c=n;for(int k=0;k<8;k++)c=(c&1)!=0?0xedb88320^(c>>1):c>>1;table[n]=c;}return table;
    }
    static uint Crc(byte[] data,int start,int count)
    {uint crc=uint.MaxValue;for(int i=start;i<start+count;i++)crc=CrcTable[(crc^data[i])&255]^(crc>>8);return crc^uint.MaxValue;}
    static Info Png(byte[] data)
    {
        int position=8,chunks=0,colorType=-1;bool header=false,palette=false,pixels=false,endedPixels=false;
        while(true)
        {
            if(++chunks>4096)Fail("PNG chunk budget exceeded");Range(data,position,12);
            uint length=BinaryPrimitives.ReadUInt32BigEndian(data.AsSpan(position,4));Range(data,position+12,length);
            if(length>MaxBytes-12)Fail("PNG chunk length limit");
            string type=Encoding.ASCII.GetString(data,position+4,4);int body=position+8,end=body+(int)length;
            uint crc=BinaryPrimitives.ReadUInt32BigEndian(data.AsSpan(end,4));
            if(Crc(data,position+4,4+(int)length)!=crc)Fail("PNG chunk CRC mismatch");
            if(type=="IHDR")
            {
                if(header||chunks!=1||length!=13)Fail("invalid PNG header");header=true;
                if(BinaryPrimitives.ReadUInt32BigEndian(data.AsSpan(body,4))!=Carrier.Width||BinaryPrimitives.ReadUInt32BigEndian(data.AsSpan(body+4,4))!=Carrier.Height)Fail("PNG dimensions differ");
                int depth=data[body+8],color=data[body+9];colorType=color;
                bool valid= color switch {0=>depth is 1 or 2 or 4 or 8 or 16,2 or 4 or 6=>depth is 8 or 16,3=>depth is 1 or 2 or 4 or 8,_=>false};
                if(!valid||data[body+10]!=0||data[body+11]!=0||data[body+12]>1)Fail("unsupported PNG sample profile");
            }
            else if(!header)Fail("PNG header must be first");
            else if(type=="PLTE")
            {if(palette||pixels||length<3||length>768||length%3!=0)Fail("invalid PNG palette");palette=true;}
            else if(type=="IDAT")
            {if(endedPixels||colorType==3&&!palette)Fail("invalid PNG image data order or missing palette");pixels=true;}
            else if(type=="IEND")
            {if(!pixels||length!=0||end+4!=data.Length)Fail("invalid PNG end");return new("PNG",1);}
            else if(type is not "tRNS" and not "gAMA" and not "pHYs" and not "sRGB" and not "cHRM")
                Fail("unsupported PNG metadata or animation chunk");
            if(type=="gAMA"&&length!=4||type=="pHYs"&&length!=9||type=="sRGB"&&length!=1||type=="cHRM"&&length!=32
               ||type=="tRNS"&&(length==0||length>256))Fail("invalid PNG metadata length");
            if(pixels&&type!="IDAT")endedPixels=true;
            position=end+4;
        }
    }
}
