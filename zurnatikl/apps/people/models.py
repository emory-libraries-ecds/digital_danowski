import itertools
from collections import defaultdict
from django.core.urlresolvers import reverse
from django.db import models
from django.utils.functional import cached_property
from django.utils.text import slugify
from igraph import Graph
import logging
from multiselectfield import MultiSelectField
import networkx as nx
import time

from zurnatikl.apps.geo.models import Location


logger = logging.getLogger(__name__)



#Schools
class SchoolManager(models.Manager):
    def get_by_natural_key(self, name):
        return self.get(name=name)


class School(models.Model):
    '''School of poetry'''

    objects = SchoolManager()

    CATEGORIZERS = {'donald-allen': 'Donald Allen'}
    # NOTE: stored value of categorizer should be written in slug
    # format, for use in network urls

    CATEGORIZER_CHOICES = ((k, v) for k, v in CATEGORIZERS.iteritems())
    # django requires list of tuple for field choices

    name = models.CharField(max_length=255)
    ''' Name of school of poetry'''
    categorizer = models.CharField(max_length=100, blank=True, choices=CATEGORIZER_CHOICES)
    '''Name of categorizer'''
    locations = models.ManyToManyField(Location, blank=True,
        related_name='schools')
    ''':class:`Location` of school of poetry'''
    notes = models.TextField(blank=True)

    def natural_key(self):
        return (self.name,)

    def __unicode__(self):
        return self.name

    class Meta:
        unique_together = ('name', 'categorizer')
        ordering = ['name']

    def location_names(self):
        return '; '.join([unicode(loc) for loc in self.locations.all()])
    location_names.short_description = u'Locations'

    @property
    def categorizer_name(self):
        'display form (non-slug) of the school categorizer'
        return self.CATEGORIZERS[self.categorizer]

    @property
    def network_id(self):
        #: node identifier when generating a network
        return 'school:%s' % self.id

    @property
    def network_attributes(self):
        #: data to be included as node attributes when generating a network
        return {
            'label': unicode(self),
            'categorizer': self.categorizer,
        }

    @property
    def has_network_edges(self):
        return self.locations.exists()

    @property
    def network_edges(self):
        #: list of tuples for edges in the network
        # tuple format:
        # source node id, target node id, optional dict of attributes (i.e. edge label)
        return [(self.network_id, loc.network_id) for loc in self.locations.all()]

    @classmethod
    def schools_network(cls, schools):
        # generate a network graph for people, places, and journals
        # associated with a set of schools (e.g., all schools categorized
        # by a particular person)

        # igraph requires numerical id; zurnatikl uses network id to
        # differentiate content types & database ids

        graph_start = time.time()
        graph = Graph()

        for s in schools:
            graph.add_vertex(s.network_id, label=unicode(s), type='School')
        info = set()
        # add people, places, & journals associated with each school

        # TODO: only add vertex to graph once!
        for s in schools:
            start = time.time()
            # a school may have one or more locations
            for loc in s.locations.all():
                graph.add_vertex(loc.network_id, label=loc.short_label,
                                 type='Place')
                info.add(loc.network_id)
                graph.add_edge(s.network_id, loc.network_id)

            # people can be associated with one or more schools
            for p in s.person_set.all():
                graph.add_vertex(p.network_id, label=p.firstname_lastname,
                                 type='Person')
                info.add(p.network_id)
                graph.add_edge(s.network_id, p.network_id)

            # journals can also be associated with a school
            for j in s.journal_set.all():
                graph.add_vertex(j.network_id, label=unicode(j),
                                 type='Journal')
                graph.add_edge(s.network_id, j.network_id)

            logger.debug('Added %d locations, %s people, and %d journals for %s in %.2f sec' % \
                (s.locations.all().count(), s.person_set.all().count(),
                 s.journal_set.all().count(), s, time.time() - start))

        logger.debug('add_vertex graph generated in %.2f sec' % (time.time() - graph_start, ))
        return graph

        # NOTE: alternate method for generating igraph
        # currently disabled; may be faster, but is also less readable
        # and has a higher likelihood of introducing errors
        # (in particular, setting attributes on the wrong nodes)
        # ---
        # node attributes can be set most effeciently by providing
        # a list; so contruct a list for each attribute for all nodes
        graph_start = time.time()
        info = defaultdict(list)

        # schools
        info['type'] = ['School'] * schools.count()
        for sch in schools:
            start = time.time()

            info['id'].append(sch.network_id)
            info['label'].append(unicode(sch))

            # a school may have one or more locations
            # only add a location if not already present,
            # because otherwise igraph will generate duplicate nodes
            new_locations = [loc for loc in sch.locations.all()
                             if loc.network_id not in info['id']]
            info['id'].extend([loc.network_id for loc in new_locations])
            info['label'].extend([loc.short_label for loc in new_locations])
            info['node'].extend(['Place'] * len(new_locations))
            # add _all_ edges
            info['edges'].extend([(sch.network_id, loc.network_id)
                                  for loc in sch.locations.all()])

            # people can be associated with one or more schools
            # identify people not already added to the list of nodes
            new_people = [p for p in sch.person_set.all()
                          if p.network_id not in info['id']]
            info['id'].extend(p.network_id for p in new_people)
            info['label'].extend(p.firstname_lastname for p in new_people)
            info['type'].extend(['Person'] * len(new_people))
            info['edges'].extend([(sch.network_id, p.network_id)
                                 for p in sch.person_set.all()])

            # journals can also be associated with a school
            new_journals = [j for j in sch.journal_set.all()
                            if j.network_id not in info['id']]
            info['id'].extend([j.network_id for j in new_journals])
            info['label'].extend([unicode(j) for j in new_journals])
            info['type'].extend(['Journal'] * len(new_journals))
            info['edges'].extend([(sch.network_id, j.network_id)
                                  for j in sch.journal_set.all()])

            logger.debug('Added %d locations, %s people, and %d journals for %s in %.2f sec' %
                         (len(new_locations), len(new_people),
                          len(new_journals), sch, time.time() - start))

        # if strings are provided, igraph treats them as names for the nodes
        start = time.time()
        graph = Graph()
        graph.add_vertices(info['id'])
        graph.vs['label'] = info['label']
        graph.vs['type'] = info['type']
        graph.add_edges(info['edges'])
        logger.debug('Added %d nodes and %d edges in %.2f sec' %
                     (len(info['id']), len(info['edges']), time.time() - start))

        logger.debug('list-based graph generated in %.2f sec' % (time.time() - graph_start, ))
        return graph


# Person and person parts
class PersonManager(models.Manager):
    def get_by_natural_key(self, first_name, last_name):
        return self.get(first_name=first_name, last_name=last_name)

class Person(models.Model):
    'A person associated with a school of poetry, journal issue, item, etc.'

    objects = PersonManager()

    #: choices for :attr:`gender`
    GENDER_CHOICES = (
        ('F', 'Female'),
        ('M', 'Male')
    )
    #: choices for :attr:`race`
    RACE_CHOICES = (
        ('American Indian or Alaska Native', 'American Indian or Alaska Native'),
        ('Asian', 'Asian'),
        ('Black or African American', 'Black or African American'),
        ('Hispanic', 'Hispanic'),
        ('Latino', 'Latino'),
        ('Native Hawaiian or Other Pacific Islander', 'Native Hawaiian or Other Pacific Islander'),
        ('White', 'White'),
    )

    #: first name
    first_name = models.CharField(max_length=100, blank=True)
    #: last name
    last_name = models.CharField(max_length=100)
    #: race
    race = MultiSelectField(max_length=200, blank=True, choices=RACE_CHOICES)
    #: race self-description
    racial_self_description = models.CharField(max_length=100, blank=True)
    #: gender
    gender = models.CharField(max_length=1, blank=True, choices=GENDER_CHOICES)
    #: schools associated with; many to many relation to :class:`School`
    schools = models.ManyToManyField('School', blank=True)
    #: uri
    uri = models.URLField(blank=True)
    #: dwelling locations
    dwellings = models.ManyToManyField(Location, blank=True,
        related_name='people')
    #: notes
    notes = models.TextField(blank=True)

    #: slug for use in urls
    slug = models.SlugField(unique=True,
        help_text='Short name for use in URLs. ' +
        'Leave blank to have a slug automatically generated. ' +
        'Change carefully, since editing this field this changes the URL on the site.',
        blank=True)
    # slug = AutoSlugField(max_length=255, unique=True,
    #     populate_from=('first_name', 'last_name'))

    class Meta:
        verbose_name_plural = u'People'
        unique_together = ('first_name', 'last_name')
        ordering = ['last_name', 'first_name']

    # available reverse relationship names:
    # - issues_edited, issues_contrib_edited
    # - items_created, items_translated, items_mentioned_in

    def save(self, force_insert=False, force_update=False, *args, **kwargs):
        # generate a slug if we don't have one set
        if self.slug is None or len(self.slug) == 0:
            max_length = Person._meta.get_field('slug').max_length
            self.slug = orig = slugify(self.firstname_lastname)[:max_length]
            for x in itertools.count(1):
                if not Person.objects.filter(slug=self.slug).exclude(pk=self.pk).exists():
                    break
                # Truncate the original slug dynamically. Minus 1 for the hyphen.
                self.slug = "%s-%d" % (orig[:max_length - len(str(x)) - 1], x)

        super(Person, self).save(force_insert, force_update, *args, **kwargs)

    def natural_key(self):
        return (self.first_name, self.last_name)

    def __unicode__(self):
        if not self.first_name:
            return self.last_name
        else:
            return '%s, %s' % (self.last_name, self.first_name)

    def get_absolute_url(self):
        return reverse('people:person', kwargs={'slug': self.slug})

    def race_label(self):
        # format list of race terms for display in admin
        if self.race:
            return ', '.join(self.race)
    race_label.short_description = "Race"

    @property
    def firstname_lastname(self):
        return ' '.join([n for n in [self.first_name, self.last_name] if n])

    @property
    def network_id(self):
        #: node identifier when generating a network
        return 'person:%s' % self.id

    @property
    def network_attributes(self):
        #: data to be included as node attributes when generating a network
        attrs = {
            'label': unicode(self),
            'last name': self.last_name,
            # yes/no flags for kinds of relations, to enable easily
            # filtering in tools like Gephi
            'editor': self.issues_edited.exists() or self.issues_contrib_edited.exists(),
            'creator': self.items_created.exists(),
            'translator': self.items_translated.exists(),
            'mentioned': self.items_mentioned_in.exists()
        }
        if self.first_name:
            attrs['first name'] = self.first_name
        if self.race:
            attrs['race'] = self.race
        if self.gender:
            attrs['gender'] = self.gender
        return attrs


    @property
    def has_network_edges(self):
        return self.schools.exists() or self.dwellings.exists()

    @property
    def network_edges(self):
        #: list of tuples for edges in the network
        # tuple format:
        # source node id, target node id, optional dict of attributes (i.e. edge label)
        # schools
        edges = [(self.network_id, school.network_id) for school in self.schools.all()]
        # dwelling locations
        edges.extend([(self.network_id, loc.network_id) for loc in self.dwellings.all()])
        return edges

    @cached_property
    def coeditors(self):
        'co-editors on the same issue'
        return Person.objects.all().filter(issues_edited__editors=self.id) \
                                   .exclude(pk=self.id).distinct()

    @cached_property
    def coauthors(self):
        'co-authors on the same item'
        return Person.objects.all().filter(items_created__creators=self.id) \
                                   .exclude(pk=self.id).distinct()

    @cached_property
    def edited_by(self):
        'authors who contributed to an issue this person edited'
        return Person.objects.all().filter(items_created__issue__editors=self.id) \
                                   .exclude(pk=self.id).distinct()

    @cached_property
    def editors(self):
        'people who edited works created by this person'
        return Person.objects.all().filter(issues_edited__item__creators=self.id) \
                                   .exclude(pk=self.id).distinct()


class NameManager(models.Manager):
    def get_by_natural_key(self, first_name, last_name, person):
        self.get(first_name=first_name, last_name=last_name)


class Name(models.Model):
    # alternate name
    # For people like LeRoi Jones / Amiri Baraka where the person
    # actually has different names during their life.
    # It would also cover maiden / married names.

    objects = NameManager()

    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100)
    person = models.ForeignKey('Person')

    def natural_key(self):
        return (self.first_name, self.last_name)

    def __unicode__(self):
        return '%s %s' % (self.first_name, self.last_name)


class PenNameManager(models.Manager):
    def get_by_natural_key(self, name):
        return self.get(name=name)


class PenName(models.Model):
    # Pen names are for someone like Mark Twain, who used a pen name
    # for publication his whole life but didn't go by it in person.

    objects = PenNameManager()

    name = models.CharField(max_length=200)
    person = models.ForeignKey('Person')

    def natural_key(self):
        return (self.name)

    def __unicode__(self):
        return self.name
