using System.Diagnostics;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using Azure.Identity;
using Microsoft.DevTunnels.Connections;
using Microsoft.DevTunnels.Contracts;
using Microsoft.DevTunnels.Management;

internal sealed record Options(string TunnelId, int RemotePort, int LocalPort, int ConnectTimeoutSeconds);

internal static class Program
{
    // Dev Tunnels uses an Entra application resource scope rather than Azure ARM.
    // The Python shell passes SP credentials through env vars and never shells out
    // to the external `devtunnel` CLI.
    private static readonly string[] TunnelScopes = new[] { "46da2f7e-b5ef-422a-88d4-2a7f9de6a0b2/.default" };

    public static async Task<int> Main(string[] args)
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

        // Keep secrets out of argv so they do not show up in process listings.
        var tenantId = Environment.GetEnvironmentVariable("EVILAZP_DEVTUNNEL_TENANT_ID");
        var clientId = Environment.GetEnvironmentVariable("EVILAZP_DEVTUNNEL_CLIENT_ID");
        var clientSecret = Environment.GetEnvironmentVariable("EVILAZP_DEVTUNNEL_CLIENT_SECRET");
        if (string.IsNullOrWhiteSpace(tenantId) || string.IsNullOrWhiteSpace(clientId) || string.IsNullOrWhiteSpace(clientSecret))
        {
            Error("authentication", "missing service-principal environment variables");
            return 2;
        }

        using var shutdown = new CancellationTokenSource();
        using var startupTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(options.ConnectTimeoutSeconds));
        using var startup = CancellationTokenSource.CreateLinkedTokenSource(shutdown.Token, startupTimeout.Token);
        var stdinTask = Task.Run(() => WatchStdinAsync(shutdown));

        Status("creating DevTunnels management client");
        await using var managementClient = CreateManagementClient(tenantId, clientId, clientSecret);
        Status("creating DevTunnels relay client");
        await using var tunnelClient = new TunnelRelayTunnelClient(managementClient, new TraceSource("evilazp-devtunnel-client"));
        tunnelClient.AcceptLocalConnectionsForForwardedPorts = false;

        TcpListener? listener = null;
        try
        {
            Status($"resolving tunnel {options.TunnelId}");
            var tunnel = await ResolveTunnelAsync(managementClient, options.TunnelId, startup.Token);
            if (tunnel == null)
            {
                Error("not-found", $"tunnel not found: {options.TunnelId}");
                return 3;
            }

            Status($"connecting tunnel {options.TunnelId}");
            await tunnelClient.ConnectAsync(tunnel, new TunnelConnectionOptions(), startup.Token);
            Status($"waiting for remote port {options.RemotePort}");
            await tunnelClient.WaitForForwardedPortAsync(options.RemotePort, startup.Token);

            // READY is the synchronization contract with the Python shell. The
            // shell only records a connection after this line appears.
            listener = new TcpListener(IPAddress.Loopback, options.LocalPort);
            listener.Start();
            Console.WriteLine($"READY local=127.0.0.1:{options.LocalPort} tunnel={options.TunnelId} port={options.RemotePort}");
            Console.Out.Flush();

            while (!shutdown.IsCancellationRequested)
            {
                var socket = await listener.AcceptSocketAsync(shutdown.Token);
                _ = Task.Run(() => HandleClientAsync(socket, tunnelClient, options.RemotePort, shutdown.Token), shutdown.Token);
            }
        }
        catch (SocketException ex) when (ex.SocketErrorCode == SocketError.AddressAlreadyInUse)
        {
            Error("local-port", $"local port already in use: {options.LocalPort}");
            return 4;
        }
        catch (AuthenticationFailedException ex)
        {
            Error("authentication", ex.Message);
            return 5;
        }
        catch (OperationCanceledException) when (!shutdown.IsCancellationRequested && startupTimeout.IsCancellationRequested)
        {
            Error("timeout", $"tunnel {options.TunnelId} did not become ready on remote port {options.RemotePort} within {options.ConnectTimeoutSeconds}s");
            return 6;
        }
        catch (OperationCanceledException)
        {
            return 0;
        }
        catch (Exception ex)
        {
            Error("connect", ex.Message);
            return 1;
        }
        finally
        {
            listener?.Stop();
            shutdown.Cancel();
            try
            {
                await stdinTask;
            }
            catch
            {
            }
        }

        return 0;
    }

    private static TunnelManagementClient CreateManagementClient(string tenantId, string clientId, string clientSecret)
    {
        var credential = new ClientSecretCredential(tenantId, clientId, clientSecret);
        return new TunnelManagementClient(
            new ProductInfoHeaderValue("evilazp-devtunnel-client", "1.0"),
            userTokenCallback: async () =>
            {
                var token = await credential.GetTokenAsync(new Azure.Core.TokenRequestContext(TunnelScopes));
                return new AuthenticationHeaderValue("Bearer", token.Token);
            },
            apiVersion: TunnelManagementClient.DefaultApiVersion);
    }

    private static async Task<Tunnel?> ResolveTunnelAsync(TunnelManagementClient client, string input, CancellationToken cancellation)
    {
        var (tunnelId, clusterId) = ParseTunnelIdentifier(input);
        var options = new TunnelRequestOptions
        {
            IncludePorts = true,
            TokenScopes = new[] { TunnelAccessScopes.Connect },
        };
        if (clusterId == null)
        {
            // Baked agents label tunnels with evilazp=<id>. Checking labels first
            // lets users pass a stable alias even if the SDK generated a suffix.
            options.Labels = new[] { $"evilazp={tunnelId}" };
            options.RequireAllLabels = true;
            var byAlias = await client.ListTunnelsAsync(clusterId: null, domain: null, options, ownedTunnelsOnly: null, cancellation);
            if (byAlias != null && byAlias.Length > 0)
            {
                return SelectBestAliasMatch(byAlias);
            }
            options.Labels = null;
            options.RequireAllLabels = false;
            return await client.GetTunnelAsync(new Tunnel { Name = tunnelId }, options, cancellation);
        }

        var tunnel = await client.GetTunnelAsync(new Tunnel { TunnelId = tunnelId, ClusterId = clusterId }, options, cancellation);
        if (tunnel != null)
        {
            return tunnel;
        }
        return await client.GetTunnelAsync(new Tunnel { Name = tunnelId }, options, cancellation);
    }

    private static Tunnel? SelectBestAliasMatch(Tunnel[] tunnels)
    {
        return tunnels
            .OrderByDescending(tunnel => tunnel.Endpoints?.Length ?? 0)
            .ThenByDescending(tunnel => tunnel.Ports?.Length ?? 0)
            .FirstOrDefault();
    }

    private static (string TunnelId, string? ClusterId) ParseTunnelIdentifier(string input)
    {
        if (Uri.TryCreate(input, UriKind.Absolute, out var uri) && uri.Host.EndsWith(".devtunnels.ms", StringComparison.OrdinalIgnoreCase))
        {
            var labels = uri.Host.Split('.');
            if (labels.Length >= 3)
            {
                return (labels[0], labels[1]);
            }
        }

        var parts = input.Split('.', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        if (parts.Length >= 2)
        {
            return (parts[0], parts[1]);
        }

        return (input, null);
    }

    private static async Task HandleClientAsync(Socket localSocket, TunnelRelayTunnelClient tunnelClient, int remotePort, CancellationToken cancellation)
    {
        await using var localStream = new NetworkStream(localSocket, ownsSocket: true);
        await using var remoteStream = await tunnelClient.ConnectToForwardedPortAsync(remotePort, cancellation);
        if (remoteStream == null)
        {
            return;
        }

        // Tunnel streams are full duplex; whichever direction finishes first
        // tears down the local connection.
        var toRemote = localStream.CopyToAsync(remoteStream, cancellation);
        var toLocal = remoteStream.CopyToAsync(localStream, cancellation);
        await Task.WhenAny(toRemote, toLocal);
    }

    private static async Task WatchStdinAsync(CancellationTokenSource shutdown)
    {
        // Python sends STOP on stdin during /devtunnels stop and shell shutdown.
        while (!shutdown.IsCancellationRequested)
        {
            var line = await Console.In.ReadLineAsync();
            if (line == null || string.Equals(line.Trim(), "STOP", StringComparison.OrdinalIgnoreCase))
            {
                shutdown.Cancel();
                return;
            }
        }
    }

    private static Options ParseOptions(string[] args)
    {
        string? tunnelId = null;
        int? remotePort = null;
        int? localPort = null;
        var connectTimeoutSeconds = 70;
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
                case "--tunnel-id":
                    tunnelId = ReadValue();
                    break;
                case "--remote-port":
                    remotePort = ParsePort(ReadValue(), "remote-port");
                    break;
                case "--local-port":
                    localPort = ParsePort(ReadValue(), "local-port");
                    break;
                case "--connect-timeout":
                    connectTimeoutSeconds = ParsePositiveInt(ReadValue(), "connect-timeout");
                    break;
                default:
                    throw new ArgumentException($"unexpected argument: {args[i]}");
            }
        }

        if (string.IsNullOrWhiteSpace(tunnelId))
        {
            throw new ArgumentException("--tunnel-id is required");
        }
        if (remotePort == null)
        {
            throw new ArgumentException("--remote-port is required");
        }

        return new Options(tunnelId, remotePort.Value, localPort ?? remotePort.Value, connectTimeoutSeconds);
    }

    private static int ParsePort(string value, string name)
    {
        if (!int.TryParse(value, out var port) || port < 1 || port > 65535)
        {
            throw new ArgumentException($"invalid {name}: {value}");
        }

        return port;
    }

    private static void Error(string code, string message)
    {
        Console.WriteLine($"ERROR {code}: {message}");
        Console.Out.Flush();
    }

    private static void Status(string message)
    {
        Console.WriteLine($"STATUS {message}");
        Console.Out.Flush();
    }

    private static int ParsePositiveInt(string value, string name)
    {
        if (!int.TryParse(value, out var parsed) || parsed < 1)
        {
            throw new ArgumentException($"invalid {name}: {value}");
        }

        return parsed;
    }
}
