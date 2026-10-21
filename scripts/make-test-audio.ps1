<#
Synthetic pt-BR audio fixtures for the long-dictation / meeting smoke tests
(docs/PLANO-REUNIOES.md, Fase 0.2), spoken by the Windows voice "Microsoft Maria Desktop".

Writes to %TEMP%\biraflow-fixtures (never into the repository; never commit audio):
  fala-30s/5min/12min/31min.wav  16 kHz mono; "Item N: <anchor word>." + filler, pauses 0.5-3 s
  fala-*.json                    {"anchors": [...]}: every anchor word spoken, in order
  reuniao-2vozes.wav             16 kHz stereo, 10 min; left = "Eu", right = "Participantes", no overlap
  reuniao-2vozes.json            {"duration": s, "segments": [{"t0", "t1", "who"}]}: who speaks when
  silencio-3min.wav              very low noise only (hallucination check)

Check with scripts\smoke.py (anchors / speakers). Deterministic (fixed random seed).
Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make-test-audio.ps1
#>
param([string]$OutDir = (Join-Path $env:TEMP 'biraflow-fixtures'))
$ErrorActionPreference = 'Stop'
# Keep this file ASCII: Windows PowerShell 5.1 reads BOM-less scripts as ANSI.

Add-Type -AssemblyName System.Speech
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
public static class FixtureWav {
    public const int Rate = 16000;
    public static void Write(string path, byte[] pcm, short channels) {
        using (var w = new BinaryWriter(File.Create(path))) {
            w.Write(Encoding.ASCII.GetBytes("RIFF")); w.Write(36 + pcm.Length);
            w.Write(Encoding.ASCII.GetBytes("WAVEfmt ")); w.Write(16); w.Write((short)1); w.Write(channels);
            w.Write(Rate); w.Write(Rate * 2 * channels); w.Write((short)(2 * channels)); w.Write((short)16);
            w.Write(Encoding.ASCII.GetBytes("data")); w.Write(pcm.Length); w.Write(pcm);
        }
    }
    public static byte[] Stereo(byte[] left, byte[] right) {
        int n = Math.Min(left.Length, right.Length) / 2;
        var o = new byte[n * 4];
        for (int i = 0; i < n; i++) {
            o[4*i] = left[2*i]; o[4*i+1] = left[2*i+1]; o[4*i+2] = right[2*i]; o[4*i+3] = right[2*i+1];
        }
        return o;
    }
    public static byte[] Noise(int samples, int amplitude, int seed) {
        var r = new Random(seed); var o = new byte[samples * 2];
        for (int i = 0; i < samples; i++) {
            short s = (short)r.Next(-amplitude, amplitude + 1); o[2*i] = (byte)s; o[2*i+1] = (byte)(s >> 8);
        }
        return o;
    }
}
'@

$BytesPerSec = 32000  # 16 kHz, 16-bit, mono
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)

function New-Voice([int]$rate) {
    $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $s.SelectVoice('Microsoft Maria Desktop')  # throws if the pt-BR voice is not installed
    $s.Rate = $rate
    $s
}

function Get-Speech($synth, [string]$text) {
    $ms = New-Object IO.MemoryStream
    $synth.SetOutputToAudioStream($ms, $fmt)
    $synth.Speak($text)
    $synth.SetOutputToNull()
    , $ms.ToArray()
}

function Add-Silence([IO.MemoryStream]$ms, [double]$seconds) {
    $n = [int]($seconds * 16000) * 2
    $ms.Write((New-Object byte[] $n), 0, $n)
}

New-Item -ItemType Directory -Force $OutDir | Out-Null
$rng = New-Object Random 42
$maria = New-Voice 0

$anchorWords = @('abacaxi', 'bicicleta', 'cachorro', 'dinossauro', 'elefante', 'foguete', 'girassol',
    'janela', 'laranja', 'martelo', 'navio', 'ovelha', 'pipoca', 'queijo', 'raposa', 'sapato', 'tomate',
    'violino', 'xadrez', 'zebra', 'borboleta', 'cenoura', 'golfinho', 'morango', 'panela', 'tartaruga',
    'vassoura', 'chocolate', 'castelo', 'montanha', 'travesseiro', 'guitarra', 'baleia', 'coelho',
    'abelha', 'tesoura', 'cadeira', 'escada', 'pirulito', 'canguru')
$fillers = @('Esta frase separa os itens da lista.', 'Agora seguimos com mais um item.',
    'Anote bem a palavra seguinte.', 'O texto continua normalmente depois da pausa.',
    'Vamos manter o mesmo ritmo de leitura.')

foreach ($f in @(@('fala-30s', 30), @('fala-5min', 300), @('fala-12min', 720), @('fala-31min', 1860))) {
    $name, $target = $f
    $ms = New-Object IO.MemoryStream
    $anchors = New-Object System.Collections.Generic.List[string]
    $n = 0
    while ($ms.Length -lt $target * $BytesPerSec) {
        $word = $anchorWords[$n % $anchorWords.Count]
        $n++
        $filler = if ($rng.Next(3) -eq 0) { $fillers[$rng.Next($fillers.Count)] } else { '' }
        $pcm = Get-Speech $maria "Item $n`: $word. $filler"
        $ms.Write($pcm, 0, $pcm.Length)
        $anchors.Add($word)
        Add-Silence $ms (2.2 * $rng.NextDouble())  # the voice already trails ~0.85 s of silence
    }
    [FixtureWav]::Write((Join-Path $OutDir "$name.wav"), $ms.ToArray(), 1)
    [IO.File]::WriteAllText((Join-Path $OutDir "$name.json"), (@{ anchors = $anchors.ToArray() } | ConvertTo-Json -Compress))
    Write-Host ("{0}.wav  {1:N1} s  {2} anchors" -f $name, ($ms.Length / $BytesPerSec), $n)
}

# Two speakers, one per channel, taking turns (never overlapping).
$me = @('Eu acho que devemos revisar o cronograma do projeto.',
    'Eu vou falar com o time de vendas na segunda feira.',
    'Eu concordo, mas precisamos de mais dados antes de decidir.',
    'Eu fico com a tarefa de atualizar a planilha de custos.',
    'Eu tenho uma pergunta sobre o prazo de entrega.',
    'Eu sugiro marcarmos outra conversa para sexta.',
    'Eu vou mandar um resumo por email depois da chamada.',
    'Eu anotei tudo e vou compartilhar com o grupo.')
$them = @('A equipe terminou a primeira parte do trabalho na semana passada.',
    'O cliente pediu para adiantar a entrega do sistema.',
    'Os testes mostraram dois erros importantes no cadastro.',
    'Podemos dividir as tarefas entre as duas equipes.',
    'O servidor novo deve chegar no fim de outubro.',
    'Temos que confirmar o valor com o setor financeiro.',
    'O documento final vai ficar pronto na quinta.',
    'Precisamos de mais tempo para revisar o contrato.')
$other = New-Voice 2  # same voice, faster, other text
$left = New-Object IO.MemoryStream
$right = New-Object IO.MemoryStream
$segments = New-Object System.Collections.Generic.List[object]
$turn = 0
while ($left.Length -lt 600 * $BytesPerSec) {
    $mine = ($turn % 2) -eq 0
    $turn++
    $lines = if ($mine) { $me } else { $them }
    $text = (1..(1 + $rng.Next(2)) | ForEach-Object { $lines[$rng.Next($lines.Count)] }) -join ' '
    $pcm = Get-Speech $(if ($mine) { $maria } else { $other }) $text
    $t0 = $left.Length / $BytesPerSec
    $speaking, $quiet = if ($mine) { $left, $right } else { $right, $left }
    $speaking.Write($pcm, 0, $pcm.Length)
    Add-Silence $quiet ($pcm.Length / $BytesPerSec)
    $segments.Add([ordered]@{ t0 = [math]::Round($t0, 2); t1 = [math]::Round($left.Length / $BytesPerSec, 2)
        who = $(if ($mine) { 'Eu' } else { 'Participantes' }) })
    $gap = 0.5 + $rng.NextDouble()
    Add-Silence $left $gap
    Add-Silence $right $gap
}
$duration = [math]::Round($left.Length / $BytesPerSec, 2)
[FixtureWav]::Write((Join-Path $OutDir 'reuniao-2vozes.wav'), [FixtureWav]::Stereo($left.ToArray(), $right.ToArray()), 2)
[IO.File]::WriteAllText((Join-Path $OutDir 'reuniao-2vozes.json'),
    (@{ duration = $duration; segments = $segments.ToArray() } | ConvertTo-Json -Depth 4))
Write-Host ("reuniao-2vozes.wav  {0:N1} s  {1} turns" -f $duration, $segments.Count)

# Amplitude 30/32768: about -60 dBFS of white noise.
[FixtureWav]::Write((Join-Path $OutDir 'silencio-3min.wav'), [FixtureWav]::Noise(180 * 16000, 30, 7), 1)
Write-Host 'silencio-3min.wav  180.0 s'
Write-Host "Fixtures em $OutDir"
