from django.views.decorators.csrf import csrf_exempt
from django.db.models import Sum
from django.http import JsonResponse, HttpResponseNotAllowed, HttpResponseBadRequest
import json
from django.core.urlresolvers import reverse
from ci import models, event
from ci.recipe import file_utils
import logging
from django.conf import settings
from datetime import timedelta
logger = logging.getLogger('ci')

def get_client_ip(request):
  x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
  if x_forwarded_for:
    ip = x_forwarded_for.split(',')[-1].strip()
  else:
    ip = request.META.get('REMOTE_ADDR')
  return ip

def ready_jobs(request, build_key, config_name, client_name):
  if request.method != 'GET':
    return HttpResponseNotAllowed(['GET'])

  client, created = models.Client.objects.get_or_create(name=client_name,ip=get_client_ip(request))
  if created:
    logger.debug('New client %s : %s seen' % (client_name, get_client_ip(request)))
  client.status_message = 'Looking for work'
  client.status = models.Client.IDLE
  client.save()

  jobs = models.Job.objects.filter(
      event__build_user__build_key=build_key,
      complete=False,
      active=True,
      ready=True,
      config__name=config_name,
      status=models.JobStatus.NOT_STARTED,
      ).order_by('-last_modified')
  jobs_json = []
  for job in jobs.all():
    data = {'id':job.pk,
        'build_key': build_key,
        'config': job.config.name,
        }
    jobs_json.append(data)

  reply = { 'jobs': jobs_json }
  return JsonResponse(reply)


def check_post(request, required_keys):
  if request.method != 'POST':
    return None, HttpResponseNotAllowed(['POST'])
  try:
    data = json.loads(request.body)
    required = set(required_keys)
    available = set(data.keys())
    if not required.issubset(available):
      logger.debug('Bad POST data.\nRequest: %s' % data)
      return data, HttpResponseBadRequest('Bad POST data')
    return data, None
  except ValueError:
    return None, HttpResponseBadRequest('Invalid JSON')


def get_job_info(job):
  job_dict = {
      'name': job.recipe.name,
      'job_id': job.pk,
      'abort_on_failure':job.recipe.abort_on_failure,
      }
  recipe_env = [
      ('job_id', job.pk),
      ('recipe_id', job.recipe.pk),
      ('comments_url', str(job.event.comments_url)),
      ('base_repo', str(job.event.base.repo())),
      ('base_ref', job.event.base.branch.name),
      ('base_sha', job.event.base.sha),
      ('base_ssh_url', str(job.event.base.ssh_url)),
      ('head_repo', str(job.event.head.repo())),
      ('head_ref', job.event.head.branch.name),
      ('head_sha', job.event.head.sha),
      ('head_ssh_url', str(job.event.head.ssh_url)),
      ('abort_on_failure', job.recipe.abort_on_failure),
      ('cause', job.recipe.cause_str()),
      ('config', job.config.name),
      ]

  for env in job.recipe.environment_vars.all():
    recipe_env.append((env.name, env.value))

  job_dict['environment'] = recipe_env

  base_file_dir = settings.RECIPE_BASE_DIR
  prestep_env = []
  for prestep in job.recipe.prestepsources.all():
    if prestep.filename:
      contents = file_utils.get_contents(base_file_dir, prestep.filename)
      if contents:
        prestep_env.append(contents)

  job_dict['prestep_sources'] = prestep_env

  step_recipes = []
  for step in job.recipe.steps.order_by('position'):
    step_dict = {
        'step_num': step.position,
        'name': step.name,
        'step_id': step.pk,
        }

    step_result, created = models.StepResult.objects.get_or_create(job=job, step=step)
    if created:
      logger.debug("Created step result %s %s" %(job, step.name))
    step_result.output = ''
    step_result.complete = False
    step_result.seconds = timedelta(seconds=0)
    step_result.status = models.JobStatus.NOT_STARTED
    step_result.save()
    step_dict['stepresult_id'] = step_result.pk

    step_env = []
    for env in step.step_environment.all():
      step_env.append((env.name, env.value))
    step_dict['environment'] = step_env
    if step.filename:
      contents = file_utils.get_contents(base_file_dir, step.filename)
      step_dict['script'] = str(contents) # in case of empty file, use str

    step_recipes.append(step_dict)
  job_dict['steps'] = step_recipes

  return job_dict



def json_claim_response(job_id, claimed, msg, job_info=None):
  return JsonResponse({
    'job_id': job_id,
    'success': claimed,
    'message': msg,
    'status': 'OK',
    'job_info': job_info,
    })

@csrf_exempt
def claim_job(request, build_key, config_name, client_name):
  data, response = check_post(request, ['job_id'])
  if response:
    return response

  try:
    config = models.BuildConfig.objects.get(name=config_name)
  except models.BuildConfig.DoesNotExist:
    err_str = 'Invalid config {}'.format(config_name)
    logger.warning(err_str)
    return HttpResponseBadRequest(err_str)

  try:
    logger.debug('trying to get job {}'.format(data['job_id']))
    job = models.Job.objects.get(pk=int(data['job_id']),
        config=config,
        event__build_user__build_key=build_key,
        status=models.JobStatus.NOT_STARTED,
        )
  except models.Job.DoesNotExist:
    logger.warning('No job found')
    return HttpResponseBadRequest('No job found')

  client_ip = get_client_ip(request)
  client, created = models.Client.objects.get_or_create(name=client_name, ip=client_ip)
  if created:
    logger.debug('New client %s : %s seen' % (client_name, client_ip))

  job.client = client
  job.status = models.JobStatus.RUNNING
  job.save()

  client.status = models.Client.RUNNING
  client.status_message = "Starting %s" % job
  client.save()

  if job.event.cause == models.Event.PULL_REQUEST:
    user = job.event.build_user
    oauth_session = user.server.auth().start_session_for_user(user)
    api = user.server.api()
    api.update_pr_status(
        oauth_session,
        job.event.base,
        job.event.head,
        api.PENDING,
        request.build_absolute_uri(reverse('ci:view_job', args=[job.pk])),
        'Starting',
        str(job),
        )
  job_info = get_job_info(job)
  return json_claim_response(job.pk, True, 'Success', job_info)

def json_finished_response(status, msg):
  return JsonResponse({'status': status, 'message': msg})


@csrf_exempt
def job_finished(request, build_key, client_name, job_id):
  data, response = check_post(request, ['seconds', 'complete'])
  if response:
    return response

  try:
    client = models.Client.objects.get(name=client_name, ip=get_client_ip(request))
  except models.Client.DoesNotExist:
    return HttpResponseBadRequest('Invalid client')

  try:
    job = models.Job.objects.get(pk=job_id, client=client, event__build_user__build_key=build_key)
  except models.Job.DoesNotExist:
    return HttpResponseBadRequest('Invalid job/build_key')

  job.seconds = timedelta(seconds=data['seconds'])
  job.complete = data['complete']
  job.status = event.job_status(job)
  job.save()

  client.status = models.Client.IDLE
  client.status_message = "Finished %s" % job
  client.save()

  if job.event.base.branch:
    job.event.base.branch.status = job.event.status
    job.event.base.branch.save()

  if job.event.cause == models.Event.PULL_REQUEST and job.status == models.JobStatus.SUCCESS:
    user = job.event.build_user
    oauth_session = user.server.auth().start_session_for_user(user)
    api = user.server.api()
    status = api.SUCCESS
    msg = 'Finished'
    api.update_pr_status(
        oauth_session,
        job.event.base,
        job.event.head,
        status,
        request.build_absolute_uri(reverse('ci:view_job', args=[job.pk])),
        msg,
        str(job),
        )

  # now check if all configs are finished
  all_complete = True
  for job in job.event.jobs.all():
    if not job.complete:
      all_complete = False
      break

  if all_complete:
    job.event.status = event.event_status(job.event)
    job.event.complete = True
    job.event.save()
  else:
    event.make_jobs_ready(job.event)
  return json_finished_response('OK', 'Success')

def json_update_response(status, msg, cmd=None):
  return JsonResponse({'status': status, 'message': msg, 'command': cmd})

def step_complete_pr_status(request, step_result, job):
  user = job.event.build_user
  server = user.server
  oauth_session = server.auth().start_session_for_user(user)
  api = server.api()
  status = api.PENDING
  desc = '(%s/%s) passed' % (step_result.step.position+1, job.recipe.steps.count())
  if job.status == models.JobStatus.CANCELED:
    status = api.ERROR
    desc = 'Canceled'

  if step_result.exit_status != 0:
    status = api.FAILURE
    desc = '{} exited with code {}'.format(step_result.step.name, step_result.exit_status)

  api.update_pr_status(
      oauth_session,
      job.event.base,
      job.event.head,
      status,
      request.build_absolute_uri(reverse('ci:view_job', args=[job.pk])),
      desc,
      str(job),
      )

@csrf_exempt
def update_step_result(request, build_key, client_name, stepresult_id):
  data, response = check_post(request,
      ['step_id', 'step_num', 'output', 'time', 'complete', 'exit_status'])
  if response:
    return response

  try:
    step_result = models.StepResult.objects.get(pk=stepresult_id)
  except models.StepResult.DoesNotExist:
    return HttpResponseBadRequest('error', 'Invalid stepresult id')

  try:
    client = models.Client.objects.get(name=client_name,ip=get_client_ip(request))
  except models.Client.DoesNotExist:
    return HttpResponseBadRequest('Invalid client')

  if client != step_result.job.client:
    return HttpResponseBadRequest('Same client that started is required')

  step_result.seconds = timedelta(seconds=data['time'])
  step_result.output = step_result.output + data['output']
  step_result.complete = data['complete']
  logger.info('Exit status: %s\n%s' % (data['exit_status'], data))
  step_result.exit_status = int(data['exit_status'])
  step_result.status = models.JobStatus.RUNNING

  client.status_msg = 'Running {}: {}'.format(step_result.job, step_result.step)

  job = step_result.job
  if data['complete']:
    step_result.output = data['output']
    if step_result.exit_status != 0:
      step_result.status = models.JobStatus.FAILED
    else:
      step_result.status = models.JobStatus.SUCCESS
    client.status_msg = 'Completed {}: {}'.format(step_result.job, step_result.step)

    if job.event.cause == models.Event.PULL_REQUEST:
      step_complete_pr_status(request, step_result, job)

  cmd = None
  if job.status == models.JobStatus.CANCELED:
    step_result.status = job.status
    cmd = 'cancel'

  client.save()
  step_result.save()
  total = job.step_results.aggregate(Sum('seconds'))
  job.seconds = total['seconds__sum']
  job.save()

  return json_update_response('OK', 'success', cmd)


