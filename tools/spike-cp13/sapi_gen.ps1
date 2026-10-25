# Speak each line with the Windows zh-TW voice, saving a 16 kHz WAV and the engine's word timing events.
param([string]$Dir, [int]$Rate = 0)
Add-Type -AssemblyName System.Speech
Add-Type -ReferencedAssemblies System.Speech -TypeDefinition @"
using System;
using System.Text;
using System.Threading;
using System.Speech.Synthesis;
using System.Speech.AudioFormat;
public static class Tts {
    public static string Run(string text, string wav, int rate) {
        var sb = new StringBuilder();
        var gate = new object();
        using (var s = new SpeechSynthesizer()) {
            s.SelectVoice("Microsoft Hanhan Desktop");
            s.Rate = rate;
            s.SetOutputToWaveFile(wav, new SpeechAudioFormatInfo(16000, AudioBitsPerSample.Sixteen, AudioChannel.Mono));
            s.SpeakProgress += (o, e) => { lock (gate) { sb.AppendFormat("{0}\t{1}\t{2}\n", e.CharacterPosition, e.CharacterCount, e.AudioPosition.TotalMilliseconds); } };
            s.Speak(text);
            Thread.Sleep(300);
        }
        lock (gate) { return sb.ToString(); }
    }
}
"@
$lines = [IO.File]::ReadAllLines((Join-Path $PSScriptRoot "lines.txt"), [Text.Encoding]::UTF8) | Where-Object { $_.Trim() }
$i = 0
foreach ($line in $lines) {
    $i++
    $name = "sapi_r{0}_{1:D2}" -f $Rate, $i
    $events = [Tts]::Run($line, (Join-Path $Dir "$name.wav"), $Rate)
    [IO.File]::WriteAllText((Join-Path $Dir "$name.tsv"), $events, (New-Object Text.UTF8Encoding $false))
}
"done $i lines at rate $Rate"
