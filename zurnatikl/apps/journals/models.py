from collections import defaultdict
from django.db import models
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.utils.text import slugify
from django.utils.safestring import mark_safe
import itertools
import logging
import networkx as nx
import time

from django_date_extensions import fields as ddx
from stdimage.models import StdImageField

from zurnatikl.apps.geo.models import Location
from zurnatikl.apps.people.models import Person, School


logger = logging.getLogger(__name__)


# for parsing natural key
class PlaceNameManager(models.Manager):
    def get_by_natural_key(self, name, location, item):
        return self.get(name=name)

class PlaceName(models.Model):
    '''Place name maps a specific :class:`~zurnatikl.apps.geo.models.Location`
    to a place as mentioned in an :class:`Item`.'''

    objects = PlaceNameManager()

    #: name
    name = models.CharField(max_length=200)
    #: :class:`zurnatikl.apps.geo.models.Location`
    location = models.ForeignKey(Location, blank=True, null=True)
    #: :class:`Item`
    item = models.ForeignKey('Item')

    # generate natural key
    def natural_key(self):
        return (self.name)

    def __unicode__(self):
        return self.name

class JournalQuerySet(models.QuerySet):

    def by_editor(self, person):
        '''Find all journals that a person edited issues for.'''
        return self.filter(issue__editors=person).distinct()

    def by_author(self, person):
        '''Find all journals that a person contributed to as an author.'''
        return self.filter(issue__item__creators=person).distinct()

    def by_editor_or_author(self, person):
        '''Find all journals that a person edited issues for or contributed
        content to as an author.'''
        return self.filter(
            models.Q(issue__editors=person) |
            models.Q(issue__contributing_editors=person) |
            models.Q(issue__item__creators=person)
            ).distinct()

# for parsing natural key
class JournalManager(models.Manager):
    def get_queryset(self):
        return JournalQuerySet(self.model, using=self._db)

    def get_by_natural_key(self, title):
        return self.get(title=title)

    def by_editor_or_author(self, person):
        return self.get_queryset().by_editor_or_author(person)

    def by_editor(self, person):
        return self.get_queryset().by_editor(person)

    def by_author(self, person):
        return self.get_queryset().by_author(person)

class Journal(models.Model):
    'A Journal or Magazine'

    objects = JournalManager()

    #: title
    title = models.CharField(max_length=255)
    #: uri
    uri = models.URLField(blank=True)
    #: publisher
    publisher = models.CharField(max_length=100, blank=True)
    #: issn
    issn = models.CharField(max_length=50, blank=True)
    #: associated schools;
    #: many-to-many to :class:`zurnatikl.apps.people.models.School`
    schools = models.ManyToManyField(School, blank=True)
    #: any additional notes
    notes = models.TextField(blank=True)
    #: slug for use in urls
    slug = models.SlugField(unique=True, blank=True,
        help_text='Short name for use in URLs. ' +
        'Leave blank to have a slug automatically generated. ' +
        'Change carefully, since editing this field this changes the site URL.')

    image = StdImageField(blank=True,
        variations={
            # sizes needed for site design use
            'thumbnail': {'width': 150, 'height': 50, 'crop': True},
            # FIXME: this size doesn't seem to be right
            'large': {'width': 942, 'height': 352, 'crop': True},
    })

    # generate natural key
    def natural_key(self):
        return (self.title,)

    def __unicode__(self):
        return self.title

    class Meta:
        ordering = ['title']

    def save(self, force_insert=False, force_update=False, *args, **kwargs):
        # generate a slug if we don't have one set
        if self.slug is None or len(self.slug) == 0:
            max_length = Journal._meta.get_field('slug').max_length
            self.slug = orig = slugify(self.title)[:max_length]
            for x in itertools.count(1):
                if not Journal.objects.filter(slug=self.slug).exclude(pk=self.pk).exists():
                    break
                # Truncate the original slug dynamically. Minus 1 for the hyphen.
                self.slug = "%s-%d" % (orig[:max_length - len(str(x)) - 1], x)

        super(Journal, self).save(force_insert, force_update, *args, **kwargs)


    def get_absolute_url(self):
        return reverse('journals:journal', kwargs={'slug': self.slug})

    def admin_thumbnail(self):
        if self.image:
            return mark_safe('<img src="%s"/>' % self.image.thumbnail.url)

    admin_thumbnail.short_description = 'thumbnail'

    @property
    def network_id(self):
        #: node identifier when generating a network
        return 'journal:%s' % self.id

    @property
    def network_attributes(self):
        #: data to be included as node attributes when generating a network
        attrs = {'label': unicode(self)}
        if self.publisher:
            attrs['publisher'] = self.publisher
        return attrs

    @property
    def has_network_edges(self):
        return self.schools.exists()

    @property
    def network_edges(self):
        #: list of tuples for edges in the network
        return [(self.network_id, school.network_id) for school in self.schools.all()]


    @classmethod
    def contributor_network(self):
        'Network graph of authors, editors, translators, and journals'

        # NOTE: this is probably a bit slow to be generating on the fly.
        # For now, cache the network after it's generated, but that
        # will need to be refined
        graph = cache.get('journal_auth_ed_network')
        if graph:
            return graph
        graph = nx.MultiGraph()
        full_start = time.time()

        start = time.time()
        journals = Journal.objects.all()
        graph.add_nodes_from(
            # node id, node attributes
            [(j.network_id, {'label': unicode(j)}) for j in journals],
            type='Journal')
        for j in journals:
            count = 0
            # editors of the journal
            editors = Person.objects.filter(issues_edited__journal=j).distinct()
            # add people to the graph
            graph.add_nodes_from(
                [(p.network_id, {'label': p.firstname_lastname}) for p in editors],
                type='Person')
            # editors are connected to the journal they edited
            graph.add_edges_from([(p.network_id, j.network_id) for p in editors],
                label='editor')
            count += editors.count()

            # authors who contributed to the journal
            authors = Person.objects.filter(items_created__issue__journal=j).distinct()
            # this could be redundant if a person was added elsewhere
            graph.add_nodes_from(
                [(p.network_id, {'label': p.firstname_lastname}) for p in authors],
                type='Person')
            count += authors.count()
            # authors are connected to the journal they contributed to
            graph.add_edges_from([(p.network_id, j.network_id) for p in authors],
                label='contributor')

            # translators who contributed to the journal
            translators = Person.objects.filter(items_translated__issue__journal=j).distinct()
            # this could be redundant if a person was added elsewhere
            graph.add_nodes_from(
                [(p.network_id, {'label': p.firstname_lastname}) for p in translators],
                type='Person')
            count += translators.count()
            # translators are connected to the journal they contributed to
            graph.add_edges_from([(p.network_id, j.network_id) for p in translators],
                label='translator')

            logger.debug('Added %d journal edges for editors/authors/translators for %s in %.2f sec' % \
                (count, j, time.time() - start))

        # co-editors
        start = time.time()
        co_editors = Person.objects.filter(issues_edited__isnull=False) \
            .annotate(editor_count=models.Count('issues_edited__editors')) \
            .filter(editor_count__gt=1).distinct()
        # for each editor, find the people they edited with
        for ed in co_editors:
            co_eds = Person.objects.filter(issues_edited__editors=ed) \
                                   .exclude(pk=ed.id).distinct()
            graph.add_edges_from([(ed.network_id, co_ed.network_id) for co_ed in co_eds],
                label='co-editor')
            # NOTE: this is redundant since we will be setting relationships
            # both directions
        logger.debug('Added co-editor edges in %.2f sec' % (time.time() - start))

        # find co-authors via items with more than one creator
        start = time.time()
        edge_count = len(graph.edges())
        coauthored_items = Item.objects.annotate(creator_count=models.Count('creators')) \
            .filter(creator_count__gt=1).distinct()
        # each item has at least two; add an edge for first and second co-author
        graph.add_edges_from([(item.creators.all()[0].network_id, item.creators.all()[1].network_id)
            for item in coauthored_items],
            label='co-author')
        # only a handful have more than two authors, so handle them separately
        for item in coauthored_items:
            if item.creators.count() > 2:
                # associate each creator with every other creator
                idx = 0
                for idx in range(item.creators.count()):
                    coauth = item.creators.all()[idx]
                    other_authors = item.creators.all()[idx+1:]
                    # for the first author, skip the second author
                    # (that edge already added for all items)
                    if idx == 0:
                         other_authors = other_authors[1:]
                    graph.add_edges_from([(coauth.network_id, creator.network_id)
                                          for creator in other_authors],
                                         label='co-author')

        logger.debug('Added %d co-author edges via items in %.2f sec' % \
            (len(graph.edges()) - edge_count, time.time() - start))

        # author/editor and translator/editor
        start = time.time()
        editors = Person.objects.filter(issues_edited__isnull=False).distinct()
        for ed in editors:
            # Note that we could do this in a single query,
            # but it seems to be faster to do separately
            authors = Person.objects.filter(items_created__issue__editors=ed.pk)
            graph.add_edges_from([(ed.network_id, person.network_id) for person in authors],
                label='edited')
            translators = Person.objects.filter(items_translated__issue__editors=ed.pk)
            graph.add_edges_from([(ed.network_id, person.network_id) for person in translators],
                label='edited')
        logger.debug('Added author/editor and translator/editor edges in %.2f sec' % (time.time() - start))


        # author/translator
        start = time.time()
        translators = Person.objects.filter(items_translated__isnull=False) \
            .distinct()
        # for each translator, find the person whose work they translated
        for translator in translators:
            authors = Person.objects.filter(items_created__translators=translator) \
                                   .exclude(pk=translator.id).distinct()
            graph.add_edges_from([(translator.network_id, auth.network_id) for auth in authors],
                label='translated')
        logger.debug('Added translator/author edges in %.2f sec' % (time.time() - start))

        # consolidate parallel edges of the same type
        start = time.time()
        edge_count = len(graph.edges())
        for node in graph.nodes_iter():
            old_edges = graph.edges(node, data=True, keys=True)
            # gather edges by label, then count edges to the same target
            edges = {}
            # source for each edge is current node
            for s, t, key, data in old_edges:
                edge_type = data['label']
                if edge_type not in edges:
                    edges[edge_type] = defaultdict(int)
                edges[edge_type][t] += 1

            # remove the old edges and add the new, consolidated ones
            graph.remove_edges_from(old_edges)
            parallels = defaultdict(int)
            for label, edges in edges.iteritems():
                for target, count in edges.iteritems():
                    parallels[target] += 1
                    graph.add_edge(node, target, label=label,
                        size=count * 10, count=parallels[target] * 3)
                    # size and count are display-specific fields for sigma.js
                    # parallel edges
                    # count indicates spacing for parallel edges
                    # size affects display size and when labels are visible
                    # NOTE: size may be set too low for smaller graphs

        logger.debug('Consolidated parallel edges (%d to %d) in %.2f sec' % \
            (edge_count, len(graph.edges()), (time.time() - start)))

        logger.debug('Complete journal contributor graph (%d nodes, %d edges) generated in %.2f sec' \
            % (len(graph.nodes()), len(graph.edges()), time.time() - full_start))

        cache.set('journal_auth_ed_network', graph)
        return graph


class IssueManager(models.Manager):
    def get_by_natural_key(self, volume, issue, season, journal):
        j = Journal.objects.get(title=journal)
        return self.get(volume=volume, issue=issue, season=season, journal=j)


class Issue(models.Model):
    'Single issue in a :class:`Journal`'

    objects = IssueManager()

    SEASON_CHOICES = (
        ('Fall', 'Fall'),
        ('Spring', 'Spring'),
        ('Summer', 'Summer'),
        ('Winter', 'Winter'),

    )

    #: :class:`Journal`
    journal = models.ForeignKey('Journal')
    #: volume number
    volume = models.CharField(max_length=255, blank=True)
    #: issue number
    issue = models.CharField(max_length=255, blank=True)
    #: publication date
    publication_date = ddx.ApproximateDateField(help_text='YYYY , MM/YYYY, DD/MM/YYYY')
    #: season of publication
    season = models.CharField(max_length=10, blank=True, choices=SEASON_CHOICES)
    #: editors, many-to-many to :class:`~zurnatikl.apps.people.models.Person`
    editors = models.ManyToManyField(Person, related_name='issues_edited')
    #: contributing editors, many-to-many to :class:`~zurnatikl.apps.people.models.Person`
    contributing_editors = models.ManyToManyField(Person,
        related_name='issues_contrib_edited', blank=True)
    #: publication address :class:`~zurnatikl.apps.geo.models.Location`
    publication_address = models.ForeignKey(Location,
        help_text="address of publication",
        related_name='issues_published_at', blank=True, null=True)
    #: print address :class:`~zurnatikl.apps.geo.models.Location`
    print_address = models.ForeignKey(Location, blank=True,
        help_text="address where issue was printed",
        related_name='issues_printed_at', null=True)
    #: mailing addresses, many-to-many relation to :class:`~zurnatikl.apps.geo.models.Location`
    mailing_addresses  = models.ManyToManyField(Location, blank=True,
        help_text="addresses where issue was mailed",
        related_name='issues_mailed_to')
    #: physical description
    physical_description = models.CharField(max_length=255, blank=True)
    #: boolean indicating if pages are numbered
    numbered_pages = models.BooleanField(default=False)
    #: price
    price = models.DecimalField(max_digits=7, decimal_places=2, blank=True, null=True)
    #: text notes
    notes = models.TextField(blank=True)
    #: issue sort order, since volume/issue/date are unreliable
    sort_order = models.PositiveSmallIntegerField("Sort order",
        blank=True, null=True,
        help_text='Sort order for display within a journal')

    class Meta:
        ordering = ['journal', 'sort_order', 'volume', 'issue']

    # generate natural key
    def natural_key(self):
        return (self.volume, self.issue, self.season, self.journal.title)

    def __unicode__(self):
        return '%s %s' % (self.journal.title, self.label)

    @property
    def label(self):
        'Issue display label without journal title'
        # format should be Volume #, Issue # (season date)
        parts = [
            'Volume %s' % self.volume if self.volume else None,
            'Issue %s' % self.issue if self.issue else 'Issue'
        ]
        return ', '.join(p for p in parts if p)

    @property
    def date(self):
        'Date for display: including publication date and season, if any'
        return ' '.join(d for d in [self.season, unicode(self.publication_date)] if d)

    def get_absolute_url(self):
        return reverse('journals:issue',
            kwargs={'journal_slug': self.journal.slug, 'id': self.id})

    @property
    def next_issue(self):
        'Next issue in order, if there is one (requires sort_order to be set)'
        if self.sort_order is not None:
            next_issues = self.journal.issue_set.all().filter(sort_order__gt=self.sort_order)
            if next_issues.exists():
                return next_issues.first()

    @property
    def previous_issue(self):
        'Previous issue in order, if there is one (requires sort_order to be set)'
        if self.sort_order is not None:
            prev_issues = self.journal.issue_set.all().filter(sort_order__lt=self.sort_order)
            if prev_issues.exists():
                return prev_issues.last()

    @property
    def network_id(self):
        #: node identifier when generating a network
        return 'issue:%s' % self.id

    @property
    def network_attributes(self):
        #: data to be included as node attributes when generating a network
        attrs = {'label': unicode(self)}
        if self.volume:
            attrs['volume'] = self.volume
        if self.issue:
            attrs['issue'] = self.issue
        if self.publication_date:
            attrs['publication date'] = unicode(self.publication_date)
        return attrs

    @property
    def has_network_edges(self):
        return any([self.journal, self.editors.exists(), self.contributing_editors.exists(),
                    self.publication_address, self.print_address, self.mailing_addresses.exists()])

    @property
    def network_edges(self):
        #: list of tuples for edges in the network
        edges = []
        if self.journal:
            edges.append((self.network_id, self.journal.network_id))
        if self.publication_address:
            edges.append((self.network_id, self.publication_address.network_id, {'label': 'publication address'}))
        if self.print_address:
            edges.append((self.network_id, self.print_address.network_id, {'label': 'print address'}))

        edges.extend([(self.network_id, ed.network_id, {'label': 'editor'})
            for ed in self.editors.all()])
        edges.extend([(self.network_id, c_ed.network_id, {'label': 'contributing editor'})
             for c_ed in self.contributing_editors.all()])
        edges.extend([(self.network_id, loc.network_id, {'label': 'mailing address'})
             for loc in self.mailing_addresses.all()])

        return edges


class GenreManager(models.Manager):
    def get_by_natural_key(self, name):
        return self.get(name=name)


class Genre(models.Model):
    'Genre'

    objects = GenreManager()

    #: name
    name = models.CharField(max_length=50)

    # generate natural key
    def natural_key(self):
        return (self.name,)

    def __unicode__(self):
        return self.name

    class Meta:
        ordering = ['name']


class ItemManager(models.Manager):
    def get_by_natural_key(self, title):
        return self.get(title=title)

class Item(models.Model):
    'Item in a :class:`Issue`'

    objects = ItemManager()

    #: :class:`Issue` the item is included in
    issue = models.ForeignKey('Issue')
    #: title
    title = models.CharField(max_length=255)
    #: creators, many-to-many to :class:`~zurnatikl.apps.people.models.Person`,
    #: related via :class:`~zurnatikl.apps.people.models.CreatorName`,
    creators = models.ManyToManyField(Person, through='CreatorName',
        related_name='items_created', blank=True)
    #: anonymous
    anonymous = models.BooleanField(help_text='check if labeled as by Anonymous',
        default=False)
    #: no creator listed
    no_creator = models.BooleanField(help_text='check if no author is listed [including Anonymous]',
        default=False)
    #: translators, :class:`~zurnatikl.apps.people.models.Person`,
    translators = models.ManyToManyField(Person,
        related_name='items_translated', blank=True)
    #: start page
    start_page = models.IntegerField()
    #: end page
    end_page = models.IntegerField()
    #: :class:`Genre`
    genre = models.ManyToManyField('Genre')
    #: includes abbreviated text
    abbreviated_text = models.BooleanField(help_text='check if the text contains abbreviations such as wd, yr, etc',
        default=False)
    #: mentioned people, many-to-many to :class:`~zurnatikl.apps.people.models.Person`
    persons_mentioned = models.ManyToManyField(Person,
        related_name='items_mentioned_in', blank=True)
    #: addressses, many-to-many to :class:`zurnatikl.apps.geo.models.Location`
    addresses = models.ManyToManyField(Location, blank=True)
    #: indicates if it is a literary advertisement
    literary_advertisement = models.BooleanField(default=False)
    #: notes
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['issue', 'start_page', 'end_page', 'title']

    # generate natural key
    def natural_key(self):
        return (self.title)

    def __unicode__(self):
        return self.title

    @property
    def edit_url(self):
        # generate a link to admin edit form for current issue item;
        # for use in various inlines, to link back to item
        return reverse('admin:%s_%s_change' % (self._meta.app_label,
                                              self._meta.model_name),
                       args=(self.id,))

    @property
    def network_id(self):
        #: node identifier when generating a network
        return 'item:%s' % self.id

    @property
    def network_attributes(self):
        #: data to be included as node attributes when generating a network
        attrs = {
            'label': self.title,
            'anonymous': self.anonymous,
            'no creator': self.no_creator,
            'issue': unicode(self.issue)
        }
        if self.genre.exists():
            attrs['genre'] = ', '.join([g.name for g in self.genre.all()])
        return attrs

    @property
    def has_network_edges(self):
        return any([self.issue, self.creators.exists(), self.translators.exists(),
                    self.persons_mentioned.exists(), self.addresses.exists(),
                    self.placename_set.exists()])

    @property
    def network_edges(self):
        #: list of tuples for edges in the network
        edges = []
        if self.issue:
            edges.append((self.network_id, self.issue.network_id))
        edges.extend([(self.network_id, c.network_id, {'label': 'creator'})
            for c in self.creators.all()])
        edges.extend([(self.network_id, trans.network_id, {'label': 'translator'})
             for trans in self.translators.all()])
        edges.extend([(self.network_id, person.network_id, {'label': 'mentioned'})
             for person in self.persons_mentioned.all()])
        edges.extend([(self.network_id, loc.network_id)
             for loc in self.addresses.all()])
        # location is not required in placenames, but only placenames with a location
        # can contribute a network edge
        edges.extend([(self.network_id, placename.location.network_id, {'label': 'mentioned'})
             for placename in self.placename_set.filter(location__isnull=False).all()
             if placename.location is not None])

        return edges


class CreatorNameManager(models.Manager):
    def get_by_natural_key(self, name_used):
        return self.get(name_used=name_used)

class CreatorName(models.Model):
    # join model for item creator,
    # with a field for capturing name as displayed on the publication

    objects = CreatorNameManager()

    item = models.ForeignKey(Item)
    person = models.ForeignKey(Person)
    name_used = models.CharField(max_length=200, blank=True)

    def natural_key(self):
        return (self.name_used,)

    def __unicode__(self):
        return unicode(self.person)
