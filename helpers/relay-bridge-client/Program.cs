using System.Net;
using System.Net.Sockets;
using Microsoft.Azure.Relay;

internal sealed record Options(
    string ConnectionString,
    IReadOnlyList<string> LocalForwards,
    IReadOnlyList<string> RemoteForwards,
    IReadOnlyList<string> RemoteHttpForwards);

internal static class Program
{
    public static int Main(string[] args)
    {
        Options options;
        try
        {
            options = ParseOptions(args);
        }
        catch (ArgumentException ex)
        {
            Error("usage", ex.Message);
            return 2;
        }

        using var shutdown = new CancellationTokenSource();
        Console.CancelKeyPress += (_, eventArgs) =>
        {
            eventArgs.Cancel = true;
            shutdown.Cancel();
        };
        AppDomain.CurrentDomain.ProcessExit += (_, _) => TryCancel(shutdown);

        RelayBridgeHost? host = null;
        try
        {
            host = new RelayBridgeHost(options, shutdown.Token);
            host.StartAsync().GetAwaiter().GetResult();

            // Python waits for READY before returning control to the interactive
            // prompt, which keeps failed helper starts from becoming orphaned.
            Console.WriteLine(
                $"READY local={options.LocalForwards.Count} remote={options.RemoteForwards.Count} http={options.RemoteHttpForwards.Count}");
            Console.Out.Flush();

            WatchStdin(shutdown);
            shutdown.Token.WaitHandle.WaitOne();
            return 0;
        }
        catch (Exception ex)
        {
            Error("start", Sanitize(ex.Message, options.ConnectionString));
            return 1;
        }
        finally
        {
            if (host != null)
            {
                try
                {
                    host.Dispose();
                }
                catch
                {
                }
            }
        }
    }

    private static void WatchStdin(CancellationTokenSource shutdown)
    {
        // The shell sends STOP over stdin for explicit stop commands and exit.
        _ = Task.Run(() =>
        {
            while (!shutdown.IsCancellationRequested)
            {
                var line = Console.In.ReadLine();
                if (line == null || string.Equals(line.Trim(), "STOP", StringComparison.OrdinalIgnoreCase))
                {
                    shutdown.Cancel();
                    return;
                }
            }
        });
    }

    private static void TryCancel(CancellationTokenSource shutdown)
    {
        try
        {
            shutdown.Cancel();
        }
        catch (ObjectDisposedException)
        {
        }
    }

    private static Options ParseOptions(string[] args)
    {
        string? connectionString = null;
        var localForwards = new List<string>();
        var remoteForwards = new List<string>();
        var remoteHttpForwards = new List<string>();

        for (var i = 0; i < args.Length; i++)
        {
            string ReadValue()
            {
                if (i + 1 >= args.Length)
                {
                    throw new ArgumentException($"missing value for {args[i]}");
                }

                return args[++i];
            }

            switch (args[i])
            {
                case "--connection-string":
                    connectionString = ReadValue();
                    break;
                case "--local-forward":
                    localForwards.Add(NormalizeLocalForward(ReadValue()));
                    break;
                case "--remote-forward":
                    remoteForwards.Add(ReadValue());
                    break;
                case "--remote-http-forward":
                    remoteHttpForwards.Add(ReadValue());
                    break;
                default:
                    throw new ArgumentException($"unexpected argument: {args[i]}");
            }
        }

        if (string.IsNullOrWhiteSpace(connectionString))
        {
            throw new ArgumentException("--connection-string is required");
        }

        if (localForwards.Count == 0 && remoteForwards.Count == 0 && remoteHttpForwards.Count == 0)
        {
            throw new ArgumentException("at least one forward is required");
        }

        return new Options(connectionString, localForwards, remoteForwards, remoteHttpForwards);
    }

    private static string NormalizeLocalForward(string value)
    {
        // Azure Relay Bridge expects an explicit host. The shell accepts
        // operator-friendly `2222:ssh` and normalizes it to loopback binding.
        var splitAt = value.LastIndexOf(':');
        if (splitAt <= 0 || splitAt == value.Length - 1)
        {
            return value;
        }

        var relayName = value[(splitAt + 1)..];
        var localSpec = value[..splitAt];
        var bindings = localSpec.Split(';', StringSplitOptions.RemoveEmptyEntries);
        if (bindings.Length == 0)
        {
            return value;
        }

        for (var i = 0; i < bindings.Length; i++)
        {
            if (IsBarePort(bindings[i]))
            {
                bindings[i] = $"127.0.0.1:{bindings[i]}";
            }
        }

        return $"{string.Join(';', bindings)}:{relayName}";
    }

    private static bool IsBarePort(string value)
    {
        var port = value.EndsWith("U", StringComparison.OrdinalIgnoreCase)
            ? value[..^1]
            : value;
        return int.TryParse(port, out var parsed) && parsed is >= 1 and <= 65535;
    }

    private static string Sanitize(string value, string connectionString)
    {
        // Helper errors can include the SAS connection string. Never echo it
        // back into the interactive shell.
        return string.IsNullOrEmpty(connectionString) ? value : value.Replace(connectionString, "[connection-string]");
    }

    private static void Error(string code, string message)
    {
        Console.WriteLine($"ERROR {code}: {message}");
        Console.Out.Flush();
    }
}

internal sealed class RelayBridgeHost : IDisposable
{
    private readonly Options _options;
    private readonly CancellationToken _shutdown;
    private readonly List<IDisposable> _disposables = new();

    public RelayBridgeHost(Options options, CancellationToken shutdown)
    {
        _options = options;
        _shutdown = shutdown;
    }

    public async Task StartAsync()
    {
        foreach (var forward in _options.LocalForwards)
        {
            StartLocalForward(forward);
        }

        foreach (var forward in _options.RemoteForwards)
        {
            await StartRemoteForwardAsync(forward).ConfigureAwait(false);
        }

        foreach (var forward in _options.RemoteHttpForwards)
        {
            await StartRemoteForwardAsync(NormalizeHttpForward(forward)).ConfigureAwait(false);
        }
    }

    private void StartLocalForward(string expression)
    {
        var (bindings, relayName) = ParseLocalForward(expression);
        foreach (var binding in bindings)
        {
            var listener = new TcpListener(binding.Address, binding.Port);
            listener.Start();
            _disposables.Add(listener);
            _ = AcceptTcpClientsAsync(listener, relayName);
        }
    }

    private async Task AcceptTcpClientsAsync(TcpListener listener, string relayName)
    {
        try
        {
            while (!_shutdown.IsCancellationRequested)
            {
                var tcpClient = await listener.AcceptTcpClientAsync().ConfigureAwait(false);
                _ = BridgeLocalClientAsync(tcpClient, relayName);
            }
        }
        catch (ObjectDisposedException)
        {
        }
        catch (SocketException) when (_shutdown.IsCancellationRequested)
        {
        }
    }

    private async Task BridgeLocalClientAsync(TcpClient tcpClient, string relayName)
    {
        using (tcpClient)
        {
            try
            {
                var relayClient = new HybridConnectionClient(_options.ConnectionString, relayName);
                using var relayStream = await relayClient.CreateConnectionAsync().ConfigureAwait(false);
                using var tcpStream = tcpClient.GetStream();
                await PumpBothWaysAsync(tcpStream, relayStream, _shutdown).ConfigureAwait(false);
            }
            catch
            {
            }
        }
    }

    private async Task StartRemoteForwardAsync(string expression)
    {
        var (relayName, host, port) = ParseRemoteForward(expression);
        var listener = new HybridConnectionListener(_options.ConnectionString, relayName);
        await listener.OpenAsync(_shutdown).ConfigureAwait(false);
        _disposables.Add(new HybridConnectionListenerDisposer(listener));
        _ = AcceptRelayClientsAsync(listener, host, port);
    }

    private async Task AcceptRelayClientsAsync(HybridConnectionListener listener, string host, int port)
    {
        try
        {
            while (!_shutdown.IsCancellationRequested)
            {
                var relayStream = await listener.AcceptConnectionAsync().ConfigureAwait(false);
                if (relayStream != null)
                {
                    _ = BridgeRelayClientAsync(relayStream, host, port);
                }
            }
        }
        catch (OperationCanceledException)
        {
        }
        catch (ObjectDisposedException)
        {
        }
    }

    private async Task BridgeRelayClientAsync(Stream relayStream, string host, int port)
    {
        using (relayStream)
        using (var tcpClient = new TcpClient())
        {
            try
            {
                await tcpClient.ConnectAsync(host, port, _shutdown).ConfigureAwait(false);
                using var tcpStream = tcpClient.GetStream();
                await PumpBothWaysAsync(relayStream, tcpStream, _shutdown).ConfigureAwait(false);
            }
            catch
            {
            }
        }
    }

    private static async Task PumpBothWaysAsync(Stream left, Stream right, CancellationToken cancellation)
    {
        var leftToRight = CopyStreamAsync(left, right, cancellation);
        var rightToLeft = CopyStreamAsync(right, left, cancellation);
        await Task.WhenAny(leftToRight, rightToLeft).ConfigureAwait(false);
    }

    private static async Task CopyStreamAsync(Stream source, Stream destination, CancellationToken cancellation)
    {
        try
        {
            await source.CopyToAsync(destination, 81920, cancellation).ConfigureAwait(false);
        }
        catch
        {
        }
    }

    private static (IReadOnlyList<IPEndPoint> Bindings, string RelayName) ParseLocalForward(string expression)
    {
        var splitAt = expression.LastIndexOf(':');
        if (splitAt <= 0 || splitAt == expression.Length - 1)
        {
            throw new ArgumentException($"invalid local Relay forward: {expression}");
        }

        var relayName = expression[(splitAt + 1)..];
        var bindingText = expression[..splitAt];
        var bindings = new List<IPEndPoint>();
        foreach (var item in bindingText.Split(';', StringSplitOptions.RemoveEmptyEntries))
        {
            bindings.Add(ParseBinding(item));
        }

        return (bindings, relayName);
    }

    private static IPEndPoint ParseBinding(string value)
    {
        if (TryParsePort(value, out var barePort))
        {
            return new IPEndPoint(IPAddress.Loopback, barePort);
        }

        var splitAt = value.LastIndexOf(':');
        if (splitAt <= 0 || splitAt == value.Length - 1)
        {
            throw new ArgumentException($"invalid local Relay binding: {value}");
        }

        var host = value[..splitAt];
        var portText = value[(splitAt + 1)..];
        if (!TryParsePort(portText, out var port))
        {
            throw new ArgumentException($"invalid local Relay port: {value}");
        }

        var address = string.Equals(host, "localhost", StringComparison.OrdinalIgnoreCase)
            ? IPAddress.Loopback
            : IPAddress.Parse(host);
        return new IPEndPoint(address, port);
    }

    private static (string RelayName, string Host, int Port) ParseRemoteForward(string expression)
    {
        var first = expression.IndexOf(':');
        var last = expression.LastIndexOf(':');
        if (first <= 0 || last <= first || last == expression.Length - 1)
        {
            throw new ArgumentException($"invalid remote Relay forward: {expression}");
        }

        var relayName = expression[..first];
        var host = expression[(first + 1)..last];
        var portText = expression[(last + 1)..];
        if (string.IsNullOrWhiteSpace(host) || !TryParsePort(portText, out var port))
        {
            throw new ArgumentException($"invalid remote Relay forward: {expression}");
        }

        return (relayName, host, port);
    }

    private static string NormalizeHttpForward(string expression)
    {
        var splitAt = expression.IndexOf(':');
        if (splitAt <= 0 || splitAt == expression.Length - 1)
        {
            throw new ArgumentException($"invalid remote HTTP Relay forward: {expression}");
        }

        var relayName = expression[..splitAt];
        var target = expression[(splitAt + 1)..];
        if (target.StartsWith("http/", StringComparison.OrdinalIgnoreCase))
        {
            return $"{relayName}:{target[5..]}";
        }

        if (target.StartsWith("https/", StringComparison.OrdinalIgnoreCase))
        {
            return $"{relayName}:{target[6..]}";
        }

        throw new ArgumentException($"invalid remote HTTP Relay forward: {expression}");
    }

    private static bool TryParsePort(string value, out int port)
    {
        if (value.EndsWith("U", StringComparison.OrdinalIgnoreCase))
        {
            value = value[..^1];
        }

        return int.TryParse(value, out port) && port is >= 1 and <= 65535;
    }

    public void Dispose()
    {
        foreach (var item in _disposables)
        {
            item.Dispose();
        }

        _disposables.Clear();
    }

    private sealed class HybridConnectionListenerDisposer : IDisposable
    {
        private readonly HybridConnectionListener _listener;

        public HybridConnectionListenerDisposer(HybridConnectionListener listener)
        {
            _listener = listener;
        }

        public void Dispose()
        {
            _listener.CloseAsync(CancellationToken.None).GetAwaiter().GetResult();
        }
    }
}
