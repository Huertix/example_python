import structlog
import json

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render
from django.views import View
from datetime import datetime, timedelta, timezone

from src.forms.upload import UploadFileForm
from src.models import Upload, Project
from src.utils.data_processing.object_architect import Architect
from src.utils.data_processing.uploader import Uploader
from src.utils.cdb_exceptions import WrongExcelFileException
from src.utils.permissions import is_user_in_groups
from src.utils.cdb_exceptions import UserNotAllowException


class UploadView(LoginRequiredMixin, View):
    form = UploadFileForm()
    logger = structlog.getLogger(__name__)

    def __days_to_next_upload(self, request):
        if request.user.is_staff:
            return 0

        last_upload = Upload.objects.all().order_by('-timestamp')
        if not last_upload:
            return 0

        return ((last_upload[0].timestamp + timedelta(days=settings.UPLOAD_ALLOWED_AFTER_DAYS)) - datetime.now(
            timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)).days

    def __check_upload_allow(self, request):

        days_to_upload = self.__days_to_next_upload(request)
        if days_to_upload > 0:
            messages.warning(
                request,
                f'A file upload is not possible at this time. Next upload will be possible after {days_to_upload} day/s')
            return False

        return True

    def get(self, request, *args, **kwargs):
        try:
            if request.user.is_superuser:
                self.form.fields['c11_file'].disabled = False
                self.form.fields['bw_file'].disabled = False
            else:
                if not self.__check_upload_allow(request):
                    self.form.fields['c11_file'].disabled = True
                    self.form.fields['bw_file'].disabled = True

                if not is_user_in_groups(request.user, ['C-Level', 'Sales']):
                    raise UserNotAllowException("User not allowed to perform this action")

        except UserNotAllowException as e:
            self.logger.error(f"User {request.user} is not allowed to see upload content")
            messages.warning(request, e.msg)
            return render(request, 'upload.html', {'form': self.form})

        except Exception as e:
            self.logger.error(
                f"Upload view error: {e}. User: {request.user}")
            messages.error(
                request, f'There was an error while fetching upload content: {e}')

        uploads = Upload.objects.all().order_by('-timestamp')
        return render(request, 'upload.html', {'form': self.form, 'uploads': uploads})

    def post(self, request, *args, **kwargs):
        form = UploadFileForm(request.POST, request.FILES)
        statistics = None
        c11_data_error = []
        bw_data_error = []
        uploader = Uploader()
        architect = Architect()

        if not self.__check_upload_allow(request) or \
                not (request.user.is_superuser or is_user_in_groups(request.user, ['C-Level', 'Sales'])):
            form.fields['c11_file'].disabled = True
            form.fields['bw_file'].disabled = True

            # TODO: add info on how excel files must be composed
            return render(request, 'upload.html', {'form': form})

        if form.is_valid():
            upload_successful = False
            file_content = ''
            c11_file = request.FILES['c11_file']
            bw_file = request.FILES['bw_file']

            try:
                c11_data, c11_data_error = uploader.parse_c11_file(file=c11_file)
                bw_data, bw_data_error = uploader.parse_bw_file(file=bw_file)
                file_content = \
                    f'{{"c11_data": {json.dumps([x.__str__() for x in bw_data])}, ' \
                        f'"bw_data": {json.dumps([x.__str__() for x in c11_data])}}}'

                statistics = architect.build_objects(c11_data, bw_data)
                upload_successful = True

                # Get extra projects in DB that was not there in upload
                # uploaded_projects_in_db = Project.objects.all()
                uploaded_order_numbers = []
                file_json_format = json.loads(file_content)
                for file_data in file_json_format.values():
                    for row in file_data:
                        order_number_cell = row[1:-1].split(',')[0]
                        uploaded_order_numbers.append(int(order_number_cell.split(':')[1]))
                extra_projects = Project.objects.exclude(order_number__in=uploaded_order_numbers)

                self.logger.info(f"Deleting {len(extra_projects)} projects in CDB that were not available in new upload: "
                                 f"{extra_projects}")
                for extra_proj_in_db in extra_projects:
                    extra_proj_in_db.delete()

            except WrongExcelFileException as e:
                messages.warning(
                    request, f'One of the files was not correctly upload. '
                             f'Please make sure files are correctly selected and composed.')

                # TODO: add info on how excel files must be composed

            except OSError as e:
                messages.warning(
                    request, f'One of the files was not an Excel file. '
                             f'Please make sure files are correctly selected and composed.')

            except Exception as e:
                self.logger.error(
                    f"Files upload error: {e}. User: {request.user}")
                messages.error(
                    request, f'There was an error while uploading files: {e}')

            finally:
                Upload(
                    creator=request.user,
                    # Force max length name to default 100 characters
                    file_name=f'c11: {c11_file.name} - bw: {bw_file.name}'[:99],
                    file_content=file_content,
                    read_successful=upload_successful,
                ).save()

                if not upload_successful:
                    return render(request, 'upload.html',
                                  {
                                    'form': form,
                                    'bw_errors': bw_data_error,
                                    'c11_errors': c11_data_error
                                  })

        else:
            return render(request, 'upload.html',
                          {
                              'form': form,
                              'bw_errors': bw_data_error,
                              'c11_errors': c11_data_error})

        return render(request, 'upload_statistics.html', {
            'data': statistics,
            'bw_errors': bw_data_error,
            'c11_errors': c11_data_error
        })

