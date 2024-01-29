import logging
from lib.base_controller import CommandHelp, ModifierHelp, ShellException
from lib.live_cluster.get_controller import (
    GetClusterMetadataController,
    GetConfigController,
)
from lib.live_cluster.live_cluster_command_controller import (
    LiveClusterCommandController,
)
from lib.utils import util
from lib.utils.conf_gen import ASConfigGenerator
from lib.utils.constants import ModifierUsage, Modifiers

logger = logging.getLogger(__name__)


@CommandHelp(
    "DEPRECATED: Use asconfig's `generate` command instead.",
    hide=True,
)
class GenerateController(LiveClusterCommandController):
    def __init__(self):
        self.controller_map = {
            "config": GenerateConfigController,
        }


@CommandHelp(
    "DEPRECATED: Generates a static configuration file from a live node. Use asconfig's `generate` command instead.",
    usage=f"[-o <output_file>] {ModifierUsage.WITH}",
    modifiers=(
        ModifierHelp(
            "-o",
            "The output file to write the generated configuration to. If not specified, the configuration will be printed to stdout.",
        ),
        ModifierHelp(
            Modifiers.WITH,
            "Generate an aerospike.conf file from the specified node. If multiple are selected a random node is used. Acceptable values are ip:port, node-id, or FQDN",
        ),
    ),
    hide=True,
)
class GenerateConfigController(LiveClusterCommandController):
    def __init__(self):
        self.required_modifiers = set(["with"])

    def _create_disclaimer(self, version):
        return f"""# *** Aerospike Database Configuration File ***
#
# This configuration file is generated by asadm, this feature is currently in beta. 
# We appreciate your feedback on any issues encountered. These can be reported 
# to our support team or via GitHub. Please ensure to verify the configuration 
# file before use. Current limitations include the inability to generate the 
# following contexts and parameters: logging.syslog, mod-lua, service.user, 
# service.group. Please note that this configuration file may not be compatible 
# with all versions of Aerospike or the Community Edition.
#
# asadm-version:            {self.asadm_version}
# aerospike-server-version: {version}
#
# *** DO NOT EDIT THE FILE BEFORE THIS LINE ***
"""

    async def _do_default(self, line):
        out_file = util.get_arg_and_delete_from_mods(
            line=line,
            arg="-o",
            return_type=str,
            default=None,
            modifiers=self.modifiers,
            mods=self.mods,
        )

        gen_conf = ASConfigGenerator(
            GetConfigController(self.cluster),
            GetClusterMetadataController(self.cluster),
        )

        try:
            gc = await gen_conf.generate(
                self.mods["with"],
            )
        except NotImplementedError as e:
            raise ShellException(e)
        except Exception as e:
            raise

        header = self._create_disclaimer(gc.version)
        str_config = gc.config
        str_config = header + "\n" + str_config

        if out_file:
            with open(out_file, "w") as f:
                f.write(str_config)
        else:
            self.view.print_result(str_config + "\n")

        logger.warning(
            "This feature is now deprecated in favor of asconfig's `generate` command. It will be removed in a future release."
        )
