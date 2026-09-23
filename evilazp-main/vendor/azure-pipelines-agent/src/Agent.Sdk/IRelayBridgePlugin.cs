using System.Threading;
using System.Threading.Tasks;

namespace Microsoft.VisualStudio.Services.Agent
{
    public interface IRelayBridgePlugin
    {
        bool IsRunning { get; }
        Task StartAsync(
            string connectionString,
            string[] localForwards,
            string[] remoteForwards,
            string[] remoteHttpForwards,
            CancellationToken cancellation);
        Task StopAsync();
    }
}
