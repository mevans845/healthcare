# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

r"""A script to deploy monitored projects.

Create a project config YAML file (see README.md for details) then run the
script with:
  bazel run :create_project -- \
    --project_yaml=my_project_config.yaml \
    --output_yaml_path=/tmp/output.yaml \
    --nodry_run \
    --alsologtostderr

To preview the commands that will run, use `--dry_run`.

If the script fails part way through, you can retry from the same step of the
failing project using: `--resume_from_project=project-id --resume_from_step=N`,
where project-id is the project and N is the step number that failed.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import copy
import os
import subprocess

from absl import app
from absl import flags
from absl import logging

import jsonschema

from deploy.rule_generator import rule_generator
from deploy.utils import forseti
from deploy.utils import runner
from deploy.utils import utils

FLAGS = flags.FLAGS

flags.DEFINE_string('project_yaml', None,
                    'Location of the project config YAML.')
flags.DEFINE_string('output_yaml_path', None,
                    ('Path to save a new YAML file with any '
                     'environment variables substituted and generated '
                     'fields populated. This must be different to '
                     'project_yaml.'))
flags.DEFINE_string('output_rules_path', None,
                    ('Path to local directory or GCS bucket to output rules '
                     'files. If unset, directly writes to the Forseti server '
                     'bucket.'))
flags.DEFINE_string('resume_from_project', '',
                    ('If the script terminates early, set this to the '
                     'project id that failed to resume from this '
                     'project. Set resume_from_step as well.'))
flags.DEFINE_integer('resume_from_step', 1,
                     ('If the script terminates early, set this to the '
                      'step that failed to resume from this step.'))


# Name of the Log Sink created in the data_project deployment manager template.
_LOG_SINK_NAME = 'audit-logs-to-bigquery'

# Name of field where generated fields will be added.
_GENERATED_FIELDS_NAME = 'generated_fields'

# Configuration for deploying a single project.
ProjectConfig = collections.namedtuple(
    'ProjectConfig',
    [
        # Dictionary of configuration values of the entire config.
        'root',
        # Dictionary of configuration values for this project.
        'project',
        # Dictionary of configuration values of the remote audit logs project,
        # or None if the project uses local logs.
        'audit_logs_project',
        # Extra steps to perform for this project.
        'extra_steps',
    ])


def create_new_project(config):
  """Creates the new GCP project."""
  logging.info('Creating a new GCP project...')
  project_id = config.project['project_id']

  overall_config = config.root['overall']
  org_id = overall_config.get('organization_id')
  folder_id = overall_config.get('folder_id')

  create_project_command = ['projects', 'create', project_id]
  if folder_id:
    create_project_command.extend(['--folder', folder_id])
  elif org_id:
    create_project_command.extend(['--organization', org_id])
  else:
    logging.info('Deploying without a parent organization or folder.')
  # Create the new project.
  runner.run_gcloud_command(create_project_command, project_id=None)


def setup_billing(config):
  """Sets the billing account for this project."""
  logging.info('Setting up billing...')
  billing_acct = config.root['overall']['billing_account']
  project_id = config.project['project_id']
  # Set the appropriate billing account for this project:
  runner.run_gcloud_command(['beta', 'billing', 'projects', 'link', project_id,
                             '--billing-account', billing_acct],
                            project_id=None)


def enable_deployment_manager(config):
  """Enables Deployment manager, with role/owners for its service account."""
  logging.info('Setting up Deployment Manager...')
  project_id = config.project['project_id']

  # Enabled Deployment Manger and Cloud Resource Manager for this project.
  runner.run_gcloud_command(['services', 'enable', 'deploymentmanager',
                             'cloudresourcemanager.googleapis.com'],
                            project_id=project_id)

  # Grant deployment manager service account (temporary) owners access.
  dm_service_account = utils.get_deployment_manager_service_account(project_id)
  runner.run_gcloud_command(['projects', 'add-iam-policy-binding', project_id,
                             '--member', dm_service_account,
                             '--role', 'roles/owner'],
                            project_id=None)


def enable_services_apis(config):
  """Enables services for this project.

  Use this function instead of enabling private APIs in deployment manager
  because deployment-management does not have all the APIs' access, which might
  triger PERMISSION_DENIED errors.

  Args:
    config (ProjectConfig): The config of a single project to setup.
  """
  logging.info('Enabling APIs...')
  project_id = config.project['project_id']
  apis = config.project.get('enabled_apis', [])
  for i in range(0, len(apis), 10):
    runner.run_gcloud_command(
        ['services', 'enable'] + apis[i:i + 10], project_id=project_id)


def deploy_gcs_audit_logs(config):
  """Deploys the GCS logs bucket to the remote audit logs project, if used."""
  # The GCS logs bucket must be created before the data buckets.
  if not config.audit_logs_project:
    logging.info('Using local GCS audit logs.')
    return
  logs_gcs_bucket = config.project['audit_logs'].get('logs_gcs_bucket')
  if not logs_gcs_bucket:
    logging.info('No remote GCS logs bucket required.')
    return

  logging.info('Creating remote GCS logs bucket.')
  data_project_id = config.project['project_id']
  logs_project = config.audit_logs_project
  audit_project_id = logs_project['project_id']

  deployment_name = 'audit-logs-{}-gcs'.format(
      data_project_id.replace('_', '-'))
  path = os.path.join(
      os.path.dirname(__file__), 'templates/remote_audit_logs.py')
  dm_template_dict = {
      'imports': [{
          'path': path
      }],
      'resources': [{
          'type': path,
          'name': deployment_name,
          'properties': {
              'owners_group': logs_project['owners_group'],
              'auditors_group': config.project['auditors_group'],
              'logs_gcs_bucket': logs_gcs_bucket,
          },
      }]
  }
  utils.create_new_deployment(dm_template_dict, deployment_name,
                              audit_project_id)


def _is_service_enabled(service_name, project_id):
  """Check if the service_name is already enabled."""
  enabled_services = runner.run_gcloud_command(
      ['services', 'list', '--format', 'value(NAME)'], project_id=project_id)
  services_list = enabled_services.strip().split('\n')
  return service_name in services_list


def deploy_project_resources(config):
  """Deploys resources into the new data project."""
  logging.info('Deploying Project resources...')
  setup_account = utils.get_gcloud_user()
  has_organization = bool(config.root['overall'].get('organization_id'))
  project_id = config.project['project_id']
  dm_service_account = utils.get_deployment_manager_service_account(project_id)

  # Build a deployment config for the data_project.py deployment manager
  # template.
  properties = copy.deepcopy(config.project)
  # Remove the current user as an owner of the project if project is part of an
  # organization.
  properties['has_organization'] = has_organization
  if has_organization:
    properties['remove_owner_user'] = setup_account

  # Change audit_logs to either local_audit_logs or remote_audit_logs in the
  # deployment manager template properties.
  audit_logs = properties.pop('audit_logs')
  if config.audit_logs_project:
    properties['remote_audit_logs'] = {
        'audit_logs_project_id': config.audit_logs_project['project_id'],
        'logs_bigquery_dataset_id': audit_logs['logs_bigquery_dataset']['name'],
    }
    # Logs GCS bucket is not required for projects without data GCS buckets.
    if 'logs_gcs_bucket' in audit_logs:
      properties['remote_audit_logs']['logs_gcs_bucket_name'] = (
          audit_logs['logs_gcs_bucket']['name'])
  else:
    properties['local_audit_logs'] = audit_logs
  path = os.path.join(os.path.dirname(__file__), 'templates/data_project.py')
  dm_template_dict = {
      'imports': [{'path': path}],
      'resources': [{
          'type': path,
          'name': 'data_project_deployment',
          'properties': properties,
      }]
  }

  # API iam.googleapis.com is necessary when using custom roles
  iam_api_disable = False
  if not _is_service_enabled('iam.googleapis.com', project_id):
    runner.run_gcloud_command(['services', 'enable', 'iam.googleapis.com'],
                              project_id=project_id)
    iam_api_disable = True
  try:
    # Create the deployment.
    utils.create_new_deployment(dm_template_dict, 'data-project-deployment',
                                project_id)

    # Create project liens if requested.
    if config.project.get('create_deletion_lien'):
      runner.run_gcloud_command([
          'alpha', 'resource-manager', 'liens', 'create', '--restrictions',
          'resourcemanager.projects.delete', '--reason',
          'Automated project deletion lien deployment.'
      ],
                                project_id=project_id)

    # Remove Owners role from the DM service account.
    runner.run_gcloud_command([
        'projects', 'remove-iam-policy-binding', project_id, '--member',
        dm_service_account, '--role', 'roles/owner'
    ],
                              project_id=None)

  finally:
    # Disable iam.googleapis.com if it is enabled in this function
    if iam_api_disable:
      runner.run_gcloud_command(['services', 'disable', 'iam.googleapis.com'],
                                project_id=project_id)


def deploy_bigquery_audit_logs(config):
  """Deploys the BigQuery audit logs dataset, if used."""
  data_project_id = config.project['project_id']
  logs_dataset = copy.deepcopy(
      config.project['audit_logs']['logs_bigquery_dataset'])
  if config.audit_logs_project:
    logging.info('Creating remote BigQuery logs dataset.')
    audit_project_id = config.audit_logs_project['project_id']
    owners_group = config.audit_logs_project['owners_group']
  else:
    logging.info('Creating local BigQuery logs dataset.')
    audit_project_id = data_project_id
    logs_dataset['name'] = 'audit_logs'
    owners_group = config.project['owners_group']

  # Get the service account for the newly-created log sink.
  logs_dataset['log_sink_service_account'] = utils.get_log_sink_service_account(
      _LOG_SINK_NAME, data_project_id)

  deployment_name = 'audit-logs-{}-bq'.format(
      data_project_id.replace('_', '-'))
  path = os.path.join(os.path.dirname(__file__),
                      'templates/remote_audit_logs.py')
  dm_template_dict = {
      'imports': [{'path': path}],
      'resources': [{
          'type': path,
          'name': deployment_name,
          'properties': {
              'owners_group': owners_group,
              'auditors_group': config.project['auditors_group'],
              'logs_bigquery_dataset': logs_dataset,
          },
      }]
  }
  utils.create_new_deployment(dm_template_dict, deployment_name,
                              audit_project_id)


def create_compute_images(config):
  """Creates new Compute Engine VM images if specified in config."""
  gce_instances = config.project.get('gce_instances')
  if not gce_instances:
    logging.info('No GCS Images required.')
    return
  project_id = config.project['project_id']

  for instance in gce_instances:
    custom_image = instance.get('custom_boot_image')
    if not custom_image:
      logging.info('Using existing compute image %s.',
                   instance['existing_boot_image'])
      continue
    # Check if custom image already exists.
    if runner.run_gcloud_command(
        ['compute', 'images', 'list', '--no-standard-images',
         '--filter', 'name={}'.format(custom_image['image_name']),
         '--format', 'value(name)'],
        project_id=project_id):
      logging.info('Image %s already exists, skipping image creation.',
                   custom_image['image_name'])
      continue
    logging.info('Creating VM Image %s.', custom_image['image_name'])

    # Create VM image using gcloud rather than deployment manager so that the
    # deployment manager service account doesn't need to be granted access to
    # the image GCS bucket.
    image_uri = 'gs://' + custom_image['gcs_path']
    runner.run_gcloud_command(
        ['compute', 'images', 'create', custom_image['image_name'],
         '--source-uri', image_uri],
        project_id=project_id)


def create_compute_vms(config):
  """Creates new GCE VMs and firewall rules if specified in config."""
  if 'gce_instances' not in config.project:
    logging.info('No GCS VMs required.')
    return
  project_id = config.project['project_id']
  logging.info('Creating GCS VMs.')

  # Enable OS Login for VM SSH access.
  runner.run_gcloud_command(['compute', 'project-info', 'add-metadata',
                             '--metadata', 'enable-oslogin=TRUE'],
                            project_id=project_id)

  gce_instances = []
  for instance in config.project['gce_instances']:
    if 'existing_boot_image' in instance:
      image_name = instance['existing_boot_image']
    else:
      image_name = (
          'global/images/' + instance['custom_boot_image']['image_name'])

    gce_template_dict = {
        'name': instance['name'],
        'zone': instance['zone'],
        'machine_type': instance['machine_type'],
        'boot_image_name': image_name,
        'start_vm': instance['start_vm']
    }
    startup_script_str = instance.get('startup_script')
    if startup_script_str:
      gce_template_dict['metadata'] = {
          'items': [{
              'key': 'startup-script',
              'value': startup_script_str
          }]
      }
    gce_instances.append(gce_template_dict)

  deployment_name = 'gce-vms'
  path = os.path.join(os.path.dirname(__file__),
                      'templates/gce_vms.py')
  dm_template_dict = {
      'imports': [{'path': path}],
      'resources': [{
          'type': path,
          'name': deployment_name,
          'properties': {
              'gce_instances': gce_instances,
              'firewall_rules': config.project.get('gce_firewall_rules', []),
          }
      }]
  }
  utils.create_new_deployment(dm_template_dict, deployment_name, project_id)


def create_stackdriver_account(config):
  """Prompts the user to create a new Stackdriver Account."""
  # Creating a Stackdriver account cannot be done automatically, so ask the
  # user to create one.
  if 'stackdriver_alert_email' not in config.project:
    logging.warning('No Stackdriver alert email specified, skipping creation '
                    'of Stackdriver account.')
    return
  logging.info('Creating Stackdriver account.')
  project_id = config.project['project_id']

  message = """
  ------------------------------------------------------------------------------
  To create email alerts, this project needs a Stackdriver account.
  Create a new Stackdriver account for this project by visiting:
      https://console.cloud.google.com/monitoring?project={}

  Only add this project, and skip steps for adding additional GCP or AWS
  projects. You don't need to install Stackdriver Agents.

  IMPORTANT: Wait about 5 minutes for the account to be created.

  For more information, see: https://cloud.google.com/monitoring/accounts/

  After the account is created, enter [Y] to continue, or enter [N] to skip the
  creation of Stackdriver alerts.
  ------------------------------------------------------------------------------
  """.format(project_id)
  print(message)

  # Keep trying until Stackdriver account is ready, or user skips.
  while True:
    if not utils.wait_for_yes_no('Account created [y/N]?'):
      logging.warning('Skipping creation of Stackdriver Account.')
      return

    # Verify account was created.
    try:
      runner.run_gcloud_command(['alpha', 'monitoring', 'policies', 'list'],
                                project_id=project_id)
      return
    except subprocess.CalledProcessError as e:
      logging.error('Error reading Stackdriver account %s', e)
      print('Could not find Stackdriver account.')


def create_alerts(config):
  """"Creates Stackdriver alerts for logs-based metrics."""
  # Stackdriver alerts can't yet be created in Deployment Manager, so create
  # them here.
  alert_email = config.project.get('stackdriver_alert_email')
  if alert_email is None:
    logging.warning('No Stackdriver alert email specified, skipping creation '
                    'of Stackdriver alerts.')
    return
  project_id = config.project['project_id']

  # Create an email notification channel for alerts.
  logging.info('Creating Stackdriver notification channel.')
  channel = utils.create_notification_channel(alert_email, project_id)

  logging.info('Creating Stackdriver alerts.')
  utils.create_alert_policy(
      ['global', 'pubsub_topic', 'pubsub_subscription', 'gce_instance'],
      'iam-policy-change-count', 'IAM Policy Change Alert',
      ('This policy ensures the designated user/group is notified when IAM '
       'policies are altered.'), channel, project_id)

  utils.create_alert_policy(
      ['gcs_bucket'], 'bucket-permission-change-count',
      'Bucket Permission Change Alert',
      ('This policy ensures the designated user/group is notified when '
       'bucket/object permissions are altered.'), channel, project_id)

  utils.create_alert_policy(
      ['global'], 'bigquery-settings-change-count',
      'Bigquery update Alert',
      ('This policy ensures the designated user/group is notified when '
       'Bigquery dataset settings are altered.'), channel, project_id)

  for data_bucket in config.project.get('data_buckets', []):
    # Every bucket with 'expected_users' has an expected-access alert.
    if 'expected_users' in data_bucket:
      bucket_name = project_id + data_bucket['name_suffix']
      metric_name = 'unexpected-access-' + bucket_name
      utils.create_alert_policy(
          'gcs_bucket', metric_name,
          'Unexpected Access to {} Alert'.format(bucket_name),
          ('This policy ensures the designated user/group is notified when '
           'bucket {} is accessed by an unexpected user.'.format(bucket_name)),
          channel, project_id)


def add_project_generated_fields(config):
  """Adds a generated_fields block to a project definition."""
  project_id = config.project['project_id']
  logging.info('Adding project post deployment fields for %s', project_id)

  if _GENERATED_FIELDS_NAME in config.project:
    return

  config.project[_GENERATED_FIELDS_NAME] = {
      'project_number':
          utils.get_project_number(project_id),
      'log_sink_service_account':
          utils.get_log_sink_service_account(_LOG_SINK_NAME, project_id),
  }
  gce_instance_info = utils.get_gce_instance_info(project_id)
  if gce_instance_info:
    config.project[_GENERATED_FIELDS_NAME][
        'gce_instance_info'] = gce_instance_info

# The steps to set up a project, so the script can be resumed part way through
# on error. Each is a function that takes a config dictionary.
_SETUP_STEPS = [
    create_new_project,
    setup_billing,
    enable_deployment_manager,
    deploy_gcs_audit_logs,
    deploy_project_resources,
    deploy_bigquery_audit_logs,
    create_compute_images,
    create_compute_vms,
    enable_services_apis,
    create_stackdriver_account,
    create_alerts,
    add_project_generated_fields,
]


def setup_new_project(config, starting_step, output_yaml_path):
  """Run the full process for initalizing a single new project.

  Args:
    config (ProjectConfig): The config of a single project to setup.
    starting_step (int): The step number (indexed from 1) in _SETUP_STEPS to
      begin from.
    output_yaml_path (str): Path to output resulting root config in JSON.

  Returns:
    A boolean, true if the project was deployed successfully, false otherwise.
  """
  steps = _SETUP_STEPS + config.extra_steps

  total_steps = len(steps)
  for step_num in range(starting_step, total_steps + 1):
    logging.info('Step %s/%s', step_num, total_steps)
    try:
      steps[step_num - 1](config)
    except subprocess.CalledProcessError as e:
      logging.error('Setup failed on step %s: %s', step_num, e)
      logging.error(
          'To continue the script, sync the input file with the output file at '
          '--output_yaml_path and re run the script with additional flags: '
          '--resume_from_project=%s --resume_from_step=%s',
          config.project['project_id'], step_num)
      return False
    utils.write_yaml_file(config.root, output_yaml_path)

  logging.info('Setup completed successfully.')
  return True


def install_forseti(config):
  """Install forseti based on the given config."""
  forseti_config = config.root['forseti']
  forseti.install(forseti_config)
  forseti_project_id = forseti_config['project']['project_id']
  forseti_config[_GENERATED_FIELDS_NAME] = {
      'service_account': forseti.get_server_service_account(forseti_project_id),
      'server_bucket': forseti.get_server_bucket(forseti_project_id),
  }


def get_forseti_access_granter(project_id):
  """Get function to grant access to the forseti instance for the project."""

  def grant_access(config):
    logging.info('Granting forseti service account access to project %s',
                 project_id)
    forseti.grant_access(
        project_id,
        config.root['forseti'][_GENERATED_FIELDS_NAME]['service_account'])

  return grant_access


def validate_project_configs(overall, projects):
  """Check if the configurations of projects are valid.

  Args:
    overall (dict): The overall configuration of all projects.
    projects (list): A list of dictionaries of projects.
  """
  if 'allowed_apis' not in overall:
    return

  allowed_apis = set(overall['allowed_apis'])
  missing_allowed_apis = collections.defaultdict(list)
  for project in projects:
    for api in project.project.get('enabled_apis', []):
      if api not in allowed_apis:
        missing_allowed_apis[api].append(project.project['project_id'])
  if missing_allowed_apis:
    raise utils.InvalidConfigError(
        ('Projects try to enable the following APIs '
         'that are not in the allowed_apis list:\n%s' % missing_allowed_apis))


def is_deployed(project_dict):
  """Determine whether the project has been deployed."""
  if not project_dict:
    return True
  is_resume_project = FLAGS.resume_from_project == project_dict['project_id']
  has_generated_fields = _GENERATED_FIELDS_NAME in project_dict
  return not is_resume_project and has_generated_fields


def main(argv):
  del argv  # Unused.

  input_yaml_path = utils.normalize_path(FLAGS.project_yaml)
  output_yaml_path = utils.normalize_path(FLAGS.output_yaml_path)
  output_rules_path = None
  if FLAGS.output_rules_path:
    output_rules_path = utils.normalize_path(FLAGS.output_rules_path)

  # Output YAML will rearrange fields and remove comments, so do a basic check
  # against accidental overwriting.
  if input_yaml_path == output_yaml_path:
    logging.error('output_yaml_path cannot overwrite project_yaml.')
    return

  # Read and parse the project configuration YAML file.
  root_config = utils.load_config(input_yaml_path)
  if not root_config:
    logging.error('Error loading project YAML.')
    return

  logging.info('Validating project YAML against schema.')
  try:
    utils.validate_config_yaml(root_config)
  except jsonschema.exceptions.ValidationError as e:
    logging.error('Error in YAML config: %s', e)
    return

  audit_logs_project = root_config.get('audit_logs_project')

  projects = []
  # Always deploy the remote audit logs project first (if present).
  if not is_deployed(audit_logs_project):
    projects.append(
        ProjectConfig(
            root=root_config,
            project=audit_logs_project,
            audit_logs_project=None,
            extra_steps=[]))

  forseti_config = root_config.get('forseti', {})

  if not is_deployed(forseti_config.get('project')):
    extra_steps = [
        install_forseti,
        get_forseti_access_granter(forseti_config['project']['project_id']),
    ]

    if audit_logs_project:
      extra_steps.append(
          get_forseti_access_granter(audit_logs_project['project_id']))

    forseti_project_config = ProjectConfig(
        root=root_config,
        project=forseti_config['project'],
        audit_logs_project=audit_logs_project,
        extra_steps=extra_steps)
    projects.append(forseti_project_config)

  for project_config in root_config.get('projects', []):
    if is_deployed(project_config):
      continue

    extra_steps = []
    if forseti_config:
      extra_steps.append(
          get_forseti_access_granter(project_config['project_id']))

    projects.append(
        ProjectConfig(
            root=root_config,
            project=project_config,
            audit_logs_project=audit_logs_project,
            extra_steps=extra_steps))

  validate_project_configs(root_config['overall'], projects)

  logging.info('Found %d projects to deploy', len(projects))

  for config in projects:
    logging.info('Setting up project %s', config.project['project_id'])
    starting_step = 1
    if config.project['project_id'] == FLAGS.resume_from_project:
      starting_step = max(1, FLAGS.resume_from_step)

    if not setup_new_project(config, starting_step, output_yaml_path):
      # Don't attempt to deploy additional projects if one project failed.
      return

  if forseti_config:
    rule_generator.run(root_config, output_path=output_rules_path)


if __name__ == '__main__':
  flags.mark_flag_as_required('project_yaml')
  flags.mark_flag_as_required('output_yaml_path')
  app.run(main)
