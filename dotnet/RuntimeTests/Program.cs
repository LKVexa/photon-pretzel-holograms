// SPDX-License-Identifier: GPL-3.0-only
// Test-only direct reference. The delivered player has no runtime reference.
using System.Text.Json;
while(Console.ReadLine() is {} input)
{
    try{Console.WriteLine("{\"ok\":true,\"receipt\":"+ArrayRuntime.Machine.Execute(input)+"}");}
    catch(Exception error){Console.WriteLine(JsonSerializer.Serialize(new {ok=false,error=error.Message}));}
}
