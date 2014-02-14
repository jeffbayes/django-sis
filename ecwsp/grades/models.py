from django.db import models
from django.db.models import Avg, Count
from django.conf import settings
from django.core.validators import MaxLengthValidator
from ecwsp.schedule.models import MarkingPeriod, Course, CourseEnrollment
from ecwsp.sis.models import Student
from ecwsp.administration.models import Configuration
from django_cached_field import CachedDecimalField

from decimal import Decimal, ROUND_HALF_UP

class GradeComment(models.Model):
    id = models.IntegerField(primary_key=True)
    comment = models.CharField(max_length=500)
    
    def __unicode__(self):
        return unicode(self.id) + ": " + unicode(self.comment)
        
    class Meta:
        ordering = ('id',)
        

def grade_comment_length_validator(value):
    max_length = int(Configuration.get_or_default('Grade comment length limit').value)
    validator = MaxLengthValidator(max_length)
    return validator(value)

class StudentMarkingPeriodGrade(models.Model):
    """ Stores marking period grades for students, only used for cache """
    student = models.ForeignKey('sis.Student')
    marking_period = models.ForeignKey(MarkingPeriod, blank=True, null=True)
    grade = CachedDecimalField(max_digits=5, decimal_places=2, blank=True, null=True, verbose_name="MP Average")
    
    class Meta:
        unique_together = ('student', 'marking_period')
    
    @staticmethod
    def build_all_cache():
        """ Create object for each student * possible marking periods """
        for student in Student.objects.all():
            marking_periods = student.courseenrollment_set.values('course__marking_period').annotate(Count('course__marking_period'))
            for marking_period in marking_periods:
                StudentMarkingPeriodGrade.objects.get_or_create(
                    student=student, marking_period_id=marking_period['course__marking_period'])
    
    def calculate_grade(self):
        # ignore overriding grades - WRONG!
        return self.student.grade_set.filter(
            letter_grade=None, grade__isnull=False, override_final=False, marking_period=self.marking_period).extra(select={
            'ave_grade':
            'AVG(grade * (select weight from schedule_markingperiod where schedule_markingperiod.id = marking_period_id))'
        }).values('ave_grade')[0]['ave_grade']
    

"""class StudentYearGrade(models.Model):
    "" Stores the grade for an entire year, only used for cache ""
    student = models.ForeignKey('sis.Student')
    year = models.ForeignKey('sis.SchoolYear')
    grade = CachedDecimalField(max_digits=5, decimal_places=2, blank=True, null=True, verbose_name="Year average")
    
    class Meta:
        unique_together = ('student', 'year')
        
    @staticmethod
    def build_all_cache():
        "" Create object for each student * possible years ""
        for student in Student.objects.all():
            years = student.courseenrollment_set.values(
                'course__marking_period__school_year').annotate(Count('course__marking_period__school_year'))
            for year in years:
                StudentYearGrade.objects.get_or_create(
                    student=student,
                    year_id=year['course__marking_period__school_year']
                )
        
    def calculate_grade(self, date_report=None):
        total = Decimal(0)
        for course_enrollment in self.courseenrollment_set.filter(
            course__marking_period__show_reports=True,
            course__marking_period__school_year=self.year
            ).distinct():
            grade = course_enrollment.calculate_real_grade(date_report=date_report, ignore_letter=True)
            if grade:
                total += grade * course_enrollment.course.credits
"""
    

class Grade(models.Model):
    student = models.ForeignKey('sis.Student')
    course = models.ForeignKey(Course)
    marking_period = models.ForeignKey(MarkingPeriod, blank=True, null=True)
    date = models.DateField(auto_now=True, validators=settings.DATE_VALIDATORS)
    grade = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)
    override_final = models.BooleanField(default=False, help_text="Override final grade for marking period instead of calculating it.")
    comment = models.CharField(max_length=500, blank=True, validators=[grade_comment_length_validator])
    letter_grade_choices = (
            ("I", "Incomplete"),
            ("P", "Pass"),
            ("F", "Fail"),
            ("A", "A"),
            ("B", "B"),
            ("C", "C"),
            ("D", "D"),
            ("HP", "High Pass"),
            ("LP", "Low Pass"),
            ("M", "Missing"),
        )
    letter_grade = models.CharField(max_length=2, blank=True, null=True, help_text="Will override grade.", choices=letter_grade_choices)
    letter_grade_behavior = {
        # Letter grade: (*normalized* value for calculations, dominate any average)
        "I": (None, True),
        "P": (1, False),
        "F": (0, False),
        # Should A be 90 or 100? A-D aren't used in calculations yet, so just omit them.
        "HP": (1, False),
        "LP": (1, False),
        "M": (0, False),
    }
    
    class Meta:
        unique_together = (("student", "course", "marking_period"),)
        permissions = (
            ("change_own_grade", "Change grades for own class"),
            ('change_own_final_grade','Change final YTD grades for own class'),
        )
        
    def display_grade(self):
        """ Returns full spelled out grade such as Fail, Pass, 60.05, B"""
        return self.get_grade(display=True)
    
    def set_grade(self, grade):
        """ set grade to decimal or letter 
            if grade is less than 1 assume it's a percentage
            returns success (True or False)"""
        try:
            grade = Decimal(str(grade))
            if grade < 1:
                # assume grade is a percentage
                grade = grade * 100
            self.grade = grade
            self.letter_grade = None
            return True
        except:
            grade = unicode.upper(unicode(grade)).strip()
            if grade in dict(self.letter_grade_choices):
                self.letter_grade = grade
                self.grade = None
                return True
            elif grade in ('', None, 'None'):
                self.grade = None
                self.letter_grade = None
                return True
            return False
        
    def invalidate_cache(self):
        """ Invalidate any related caches """
        try:
            enrollment = self.course.courseenrollment_set.get(user=self.student, role="student")
            enrollment.flag_grade_as_stale()
            enrollment.flag_numeric_grade_as_stale()
        except CourseEnrollment.DoesNotExist:
            pass
        self.student.cache_gpa = self.student.calculate_gpa()
        if self.student.cache_gpa != "N/A":
            self.student.save()
    
    def get_grade(self, letter=False, display=False, rounding=None, minimum=None):
        """
        letter: Does nothing?
        display: For letter grade - Return display name instead of abbreviation.
        rounding: Numeric - round to this many decimal places.
        minimum: Numeric - Minimum allowed grade. Will not return lower than this.
        Returns grade such as 90.03, P, or F
        """
        if self.letter_grade:
            if display:
                return self.get_letter_grade_display()
            else:
                return self.letter_grade
        elif self.grade:
            grade = self.grade
            if minimum:
                if grade < minimum:
                    grade = minimum
            if rounding != None:
                string = '%.' + str(rounding) + 'f'
                grade = string % float(str(grade))
            return grade
        else:
            return ""
    
    def clean(self):
        from django.core.exceptions import ValidationError
        if self.grade and self.letter_grade != None:
            raise ValidationError('Cannot have both numeric and letter grade.')
    
    def save(self, *args, **kwargs):
        super(Grade, self).save(*args, **kwargs)
        self.invalidate_cache()
    
    def delete(self, *args, **kwargs):
        self.invalidate_cache()
        super(Grade, self).delete(*args, **kwargs)

    def __unicode__(self):
        return unicode(self.get_grade(self))
