# Builds build\ScrapVR-Pose.apk with the bare SDK tools (no Gradle, no resources).
# Needs the Android 10 platform jar once:
#   sdkmanager --sdk_root=<Sdk> "platforms;android-29"
# Install: adb install -r android\build\ScrapVR-Pose.apk
param(
    [string]$Sdk = $env:ANDROID_SDK_ROOT,   # needs build-tools\35.0.0 and platforms\android-29
    [string]$Jdk = $env:JAVA_HOME           # JDK 17
)
if (-not $Sdk -or -not $Jdk) { throw "Pass -Sdk <Android SDK> -Jdk <JDK 17>, or set ANDROID_SDK_ROOT and JAVA_HOME." }
# No ErrorActionPreference=Stop: javac/d8 print warnings on stderr, which PowerShell 5.1
# would turn into errors. Every tool is checked through $LASTEXITCODE instead.
$env:JAVA_HOME = $Jdk
$env:PATH = "$Jdk\bin;$env:PATH"
$tools = "$Sdk\build-tools\35.0.0"
$jar = "$Sdk\platforms\android-29\android.jar"
$out = "$PSScriptRoot\build"
$keystore = "$PSScriptRoot\debug.keystore"
if (-not (Test-Path $jar)) { throw "Missing $jar - run: sdkmanager --sdk_root=`"$Sdk`" `"platforms;android-29`"" }

if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force "$out\classes" | Out-Null

& "$tools\aapt2.exe" link --manifest "$PSScriptRoot\AndroidManifest.xml" -I $jar -o "$out\base.apk"
if ($LASTEXITCODE) { throw "aapt2 link failed" }
$sources = (Get-ChildItem "$PSScriptRoot\src" -Recurse -Filter *.java).FullName
& "$Jdk\bin\javac.exe" -source 8 -target 8 -bootclasspath $jar -d "$out\classes" $sources
if ($LASTEXITCODE) { throw "javac failed" }
$classes = (Get-ChildItem "$out\classes" -Recurse -Filter *.class).FullName
& "$tools\d8.bat" --min-api 26 --lib $jar --output $out $classes
if ($LASTEXITCODE) { throw "d8 failed" }

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::Open("$out\base.apk", "Update")
[IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, "$out\classes.dex", "classes.dex") | Out-Null
$zip.Dispose()

& "$tools\zipalign.exe" -f -p 4 "$out\base.apk" "$out\aligned.apk"
if ($LASTEXITCODE) { throw "zipalign failed" }
if (-not (Test-Path $keystore)) {
    & "$Jdk\bin\keytool.exe" -genkeypair -keystore $keystore -storepass android -keypass android `
        -alias scrapvr -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=ScrapVR Dev"
    if ($LASTEXITCODE) { throw "keytool failed" }
}
& "$tools\apksigner.bat" sign --ks $keystore --ks-pass pass:android --out "$out\ScrapVR-Pose.apk" "$out\aligned.apk"
if ($LASTEXITCODE) { throw "apksigner failed" }
Write-Output "Built $out\ScrapVR-Pose.apk"
