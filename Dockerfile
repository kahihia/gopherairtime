FROM ubuntu:precise

RUN apt-get update
RUN apt-get install -y python
RUN apt-get install -y python-pip

RUN apt-get install -y nginx
RUN apt-get install -y supervisor

# libpq needed for psycopg2
RUN apt-get install -y libpq-dev python-dev

RUN mkdir -p /srv/wcl/prod/gopherairtime
ADD . /srv/wcl/prod/gopherairtime

RUN pip install -r /srv/wcl/prod/gopherairtime/requirements.pip

RUN echo "daemon off;" >> /etc/nginx/nginx.conf
RUN rm /etc/nginx/sites-enabled/default
RUN cp /srv/wcl/prod/gopherairtime/gopherairtime.com.conf /etc/nginx/sites-enabled/
RUN cp /srv/wcl/prod/gopherairtime/etc/supervisord.conf /etc/supervisord.conf

VOLUME ["/srv/wcl/prod/gopherairtime/logs/"]

EXPOSE 80
CMD /srv/wcl/prod/gopherairtime/run.sh
