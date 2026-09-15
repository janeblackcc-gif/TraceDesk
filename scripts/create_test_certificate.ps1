$ErrorActionPreference = 'Stop'
$testCertificateDirectory = Join-Path $PSScriptRoot '../artifacts/tls-fixture'
[System.IO.Directory]::CreateDirectory($testCertificateDirectory) | Out-Null
$testKeyPath = Join-Path $testCertificateDirectory 'localhost-key.pem'
$testCertificatePath = Join-Path $testCertificateDirectory 'localhost-cert.pem'
if ((Test-Path -LiteralPath $testKeyPath) -or (Test-Path -LiteralPath $testCertificatePath)) {
    throw 'Test certificate already exists; preserve it or choose a new fixture directory.'
}
$testRsa = [System.Security.Cryptography.RSA]::Create(2048)
try {
    $testRequest = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
        'CN=localhost', $testRsa, [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $testSan = [System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
    $testSan.AddDnsName('localhost')
    $testSan.AddIpAddress([System.Net.IPAddress]::Loopback)
    $testRequest.CertificateExtensions.Add($testSan.Build())
    $testCertificate = $testRequest.CreateSelfSigned([DateTimeOffset]::UtcNow.AddMinutes(-5), [DateTimeOffset]::UtcNow.AddDays(2))
    try {
        [System.IO.File]::WriteAllText($testKeyPath, $testRsa.ExportPkcs8PrivateKeyPem(), [System.Text.UTF8Encoding]::new($false))
        [System.IO.File]::WriteAllText($testCertificatePath, $testCertificate.ExportCertificatePem(), [System.Text.UTF8Encoding]::new($false))
    } finally { $testCertificate.Dispose() }
} finally { $testRsa.Dispose() }
Write-Output 'Created a short-lived localhost certificate for isolated browser acceptance. No trust store was changed.'
