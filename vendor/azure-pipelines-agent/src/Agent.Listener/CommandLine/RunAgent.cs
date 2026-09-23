using CommandLine;
using Microsoft.VisualStudio.Services.Agent;

namespace Agent.Listener.CommandLine
{
    // Default Non-Requried Verb
    [Verb(Constants.Agent.CommandLine.Commands.Run)]
    public class RunAgent : ConfigureOrRemoveBase
    {
        [Option(Constants.Agent.CommandLine.Args.Agent)]
        public string Agent { get; set; }

        [Option(Constants.Agent.CommandLine.Flags.Commit)]
        public bool Commit { get; set; }

        [Option(Constants.Agent.CommandLine.Flags.Diagnostics)]
        public bool Diagnostics { get; set; }

        [Option(Constants.Agent.CommandLine.Args.Pool)]
        public string Pool { get; set; }

        [Option(Constants.Agent.CommandLine.Flags.Replace)]
        public bool Replace { get; set; }

        [Option(Constants.Agent.CommandLine.Flags.Once)]
        public bool RunOnce { get; set; }

        [Option(Constants.Agent.CommandLine.Args.StartupType)]
        public string StartupType { get; set; }

        [Option(Constants.Agent.CommandLine.Flags.DebugMode)]
        public bool DebugMode { get; set; }

        [Option(Constants.Agent.CommandLine.Args.Url)]
        public string Url { get; set; }

        [Option(Constants.Agent.CommandLine.Args.Work)]
        public string Work { get; set; }
    }
}
